"""
Alpha Council v2.4 - Evidence Package construction.

Each agent receives only the sections its role needs, capped in tokens.
Two properties matter:

  ORDERING. Stable text first, dynamic evidence last. Prompt caching keys
  on a shared prefix, so putting the volatile parts at the end is worth
  real money across a session.

  HONESTY ABOUT PRICES. Every option field carries quote_lag_seconds,
  raw_mid, adjusted_mid and stale_adjusted. Indicative quotes are derived
  estimates, not OPRA NBBO, and the agents are told so explicitly rather
  than being left to assume the numbers are executable.

No agent ever sees a contract the options engine did not construct.

Place at: alpha_council/agents/evidence.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Sequence

from alpha_council.models.candidate import AnalystAssessment, CandidateFeatures
from alpha_council.models.enums import CandidateTrack
from alpha_council.models.intelligence import IntelligenceEvent
from alpha_council.models.trading import OptionStructure, PortfolioProposal
from alpha_council.utils.ids import input_hash
from alpha_council.utils.time import iso_utc

# Rough but stable: ~4 characters per token for English JSON.
CHARS_PER_TOKEN = 4

AgentRole = Literal["BULL", "BEAR", "CATALYST", "PM", "SELECTION",
                    "RED_TEAM", "REVISION"]

DATA_CAVEAT = (
    "The option prices in this package come from Alpaca's Indicative Pricing "
    "Feed, not OPRA NBBO. A quote timestamp may be fresh while the quoted "
    "value is still a derived estimate. Each leg carries quote_lag_seconds, "
    "raw_mid, adjusted_mid and stale_adjusted; when stale_adjusted is true "
    "the price was adjusted for underlying movement since the quote "
    "timestamp. Treat all option prices as estimates and do not infer "
    "intraday option-price momentum from them."
)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _truncate_events(events: Sequence[IntelligenceEvent],
                     limit: int) -> list[dict[str, Any]]:
    ordered = sorted(events, key=lambda e: e.catalyst_score, reverse=True)
    return [{
        "event_id": e.event_id,
        "type": e.event_type,
        "direction": str(e.direction),
        "direction_confidence": round(e.direction_confidence, 2),
        "catalyst": round(e.catalyst_score, 1),
        "materiality": round(e.materiality_score, 1),
        "freshness": round(e.freshness_score, 1),
        "novelty": round(e.novelty_score, 1),
        "corroboration": round(e.corroboration_score, 1),
        "surprise": round(e.surprise_score, 1),
        "market_confirmation": round(e.market_confirmation_score, 1),
        "provisional": e.provisional,
        "facts": e.extracted_facts[:4],
        "sources": e.evidence_urls[:3],
    } for e in ordered[:limit]]


def _structure_dict(s: OptionStructure) -> dict[str, Any]:
    return {
        "rank": s.rank,
        "strategy": str(s.strategy),
        "expiration": s.expiration.isoformat(),
        "dte": s.dte,
        "long": {"symbol": s.long_leg.symbol, "strike": s.long_leg.strike,
                 "delta": round(s.long_leg.delta, 3),
                 "bid": s.long_leg.bid, "ask": s.long_leg.ask,
                 "raw_mid": round(s.long_leg.raw_mid, 2),
                 "adjusted_mid": round(s.long_leg.adjusted_mid, 2),
                 "quote_lag_seconds": round(s.long_leg.quote_lag_seconds, 1),
                 "open_interest": s.long_leg.open_interest,
                 "volume": s.long_leg.volume,
                 "iv": s.long_leg.implied_volatility},
        "short": {"symbol": s.short_leg.symbol, "strike": s.short_leg.strike,
                  "delta": round(s.short_leg.delta, 3),
                  "bid": s.short_leg.bid, "ask": s.short_leg.ask,
                  "raw_mid": round(s.short_leg.raw_mid, 2),
                  "adjusted_mid": round(s.short_leg.adjusted_mid, 2),
                  "quote_lag_seconds": round(s.short_leg.quote_lag_seconds, 1),
                  "open_interest": s.short_leg.open_interest,
                  "volume": s.short_leg.volume,
                  "iv": s.short_leg.implied_volatility},
        "width": s.width,
        "limit_debit": s.initial_limit_debit,
        "natural_debit": round(s.natural_debit, 2),
        "cost_to_width": round(s.cost_to_width_ratio, 3),
        "max_loss_per_spread": s.max_loss_per_spread,
        "max_profit_per_spread": s.max_profit_per_spread,
        "reward_risk": round(s.reward_risk_ratio, 2),
        "breakeven": round(s.breakeven, 2),
        "staleness_buffer": s.staleness_buffer,
        "stale_adjusted": s.stale_adjusted,
        "max_quote_lag_seconds": round(s.max_quote_lag_seconds, 1),
        "scores": {"structure": round(s.structure_score, 1),
                   "liquidity": round(s.liquidity_score, 1),
                   "delta_fit": round(s.delta_fit_score, 1),
                   "dte_fit": round(s.dte_fit_score, 1),
                   "cost_efficiency": round(s.cost_efficiency_score, 1)},
    }


@dataclass(slots=True)
class EvidencePackage:
    symbol: str
    as_of: datetime
    role: AgentRole
    sections: dict[str, Any] = field(default_factory=dict)
    token_estimate: int = 0
    truncated: list[str] = field(default_factory=list)

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.sections, default=str, indent=indent,
                          sort_keys=True)

    @property
    def content_hash(self) -> str:
        return input_hash(self.to_json())


class EvidenceBuilder:
    """Builds role-scoped packages from a single set of inputs."""

    def __init__(self, candidate: CandidateFeatures,
                 intel_events: Sequence[IntelligenceEvent] = (),
                 structures: Sequence[OptionStructure] = (),
                 portfolio_state: dict[str, Any] | None = None,
                 market_summary: dict[str, Any] | None = None,
                 scheduled_events: Sequence[dict[str, Any]] = (),
                 session_briefing: str | None = None):
        self.c = candidate
        self.events = list(intel_events)
        self.structures = list(structures)
        self.portfolio = portfolio_state or {}
        self.market = market_summary or {}
        self.scheduled = list(scheduled_events)
        self.briefing = session_briefing

    # ---- shared sections ------------------------------------------

    def _header(self) -> dict[str, Any]:
        return {
            "symbol": self.c.symbol,
            "timestamp": iso_utc(self.c.as_of),
            "track": str(self.c.track),
            "discovery_source": str(self.c.discovery_source),
            "proposed_direction": str(self.c.direction),
            "data_caveat": DATA_CAVEAT,
        }

    def _features(self) -> dict[str, Any]:
        out = {
            "momentum": round(self.c.momentum_score, 1),
            "relative_volume": round(self.c.relative_volume_score, 1),
            "trend_regime": round(self.c.trend_regime_score, 1),
            "relative_strength": round(self.c.relative_strength_score, 1),
            "options_opportunity": round(self.c.options_opportunity_score, 1),
            "options_liquidity": round(self.c.options_liquidity_score, 1),
            "combined_direction": round(self.c.combined_direction, 3),
            "final_opportunity_score": round(self.c.final_opportunity_score, 1),
            "data_confidence_factor": self.c.data_confidence_factor,
            "regime_factor": self.c.regime_factor,
            "key_metrics": self.c.key_metrics,
        }
        if self.c.track is CandidateTrack.EVENT:
            out.update({"catalyst": round(self.c.catalyst_score or 0, 1),
                        "corroboration": round(self.c.corroboration_score or 0, 1),
                        "novelty": round(self.c.novelty_score or 0, 1)})
        else:
            # State the absence rather than omitting the key, so the agent
            # knows there is no catalyst instead of inferring one is missing.
            out["catalyst"] = None
            out["catalyst_note"] = (
                "MOMENTUM track: no material catalyst was identified. This is "
                "an absence of evidence, not evidence of a neutral catalyst.")
        return out

    # ---- role packages ---------------------------------------------

    def build(self, role: AgentRole, cap_tokens: int = 3500,
              analyst_outputs: Sequence[AnalystAssessment] = (),
              proposal: PortfolioProposal | None = None,
              selected_rank: int | None = None,
              red_team_summary: dict[str, Any] | None = None
              ) -> EvidencePackage:
        pkg = EvidencePackage(symbol=self.c.symbol, as_of=self.c.as_of,
                              role=role)
        s = pkg.sections
        s["context"] = self._header()
        s["candidate_features"] = self._features()

        if self.briefing and role in ("PM", "CATALYST", "RED_TEAM"):
            s["session_briefing"] = self.briefing

        event_limit = {"CATALYST": 10, "BULL": 5, "BEAR": 5,
                       "PM": 6, "RED_TEAM": 8}.get(role, 4)
        if self.events:
            s["intelligence_events"] = _truncate_events(self.events, event_limit)
            if len(self.events) > event_limit:
                pkg.truncated.append(
                    f"intelligence_events {len(self.events)}->{event_limit}")
        else:
            s["intelligence_events"] = []

        if role in ("BULL", "BEAR", "CATALYST", "PM", "RED_TEAM"):
            s["market_summary"] = self.market

        if role in ("PM", "RED_TEAM"):
            s["portfolio_state"] = self.portfolio
            s["scheduled_event_risk"] = self.scheduled[:5]

        if role in ("SELECTION", "RED_TEAM", "REVISION"):
            s["top_option_structures"] = [
                _structure_dict(x) for x in self.structures[:5]]

        if analyst_outputs and role in ("PM", "RED_TEAM"):
            s["analyst_assessments"] = [{
                "analyst": a.analyst, "score": round(a.score, 1),
                "confidence": round(a.confidence, 2), "thesis": a.thesis,
                "evidence_for": a.evidence_for[:4],
                "evidence_against": a.evidence_against[:4],
                "missing_information": a.missing_information[:3],
                "invalidation_conditions": a.invalidation_conditions[:3],
            } for a in analyst_outputs]

        if proposal is not None and role in ("SELECTION", "RED_TEAM",
                                             "REVISION"):
            s["pm_proposal"] = {
                "trade": proposal.trade,
                "direction": str(proposal.direction),
                "confidence": round(proposal.confidence, 2),
                "horizon_days": proposal.expected_horizon_days,
                "desired_risk_pct": proposal.desired_portfolio_risk_pct,
                "thesis": proposal.thesis,
                "catalyst_summary": proposal.catalyst_summary,
                "supporting": proposal.key_supporting_evidence[:5],
                "contrary": proposal.key_contrary_evidence[:5],
                "invalidation": [r.model_dump() for r in proposal.invalidation],
                "selected_structure_rank": selected_rank
                or proposal.selected_structure_rank,
            }

        if red_team_summary and role == "REVISION":
            s["red_team_review"] = red_team_summary

        pkg.token_estimate = estimate_tokens(pkg.to_json())
        if pkg.token_estimate > cap_tokens:
            self._shrink(pkg, cap_tokens)
        return pkg

    @staticmethod
    def _shrink(pkg: EvidencePackage, cap_tokens: int) -> None:
        """Drop the least decision-relevant material first.

        Never drops candidate_features, the data caveat, or the option
        structures: those are what the agent is being asked about.
        """
        order = ["session_briefing", "market_summary", "scheduled_event_risk",
                 "intelligence_events", "analyst_assessments"]
        for key in order:
            if pkg.token_estimate <= cap_tokens:
                return
            if key not in pkg.sections:
                continue
            value = pkg.sections[key]
            if isinstance(value, list) and len(value) > 1:
                pkg.sections[key] = value[: max(1, len(value) // 2)]
                pkg.truncated.append(f"{key} halved")
            else:
                pkg.sections.pop(key)
                pkg.truncated.append(f"{key} dropped")
            pkg.token_estimate = estimate_tokens(pkg.to_json())
