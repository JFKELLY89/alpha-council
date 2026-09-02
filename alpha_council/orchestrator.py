"""
Alpha Council v2.5 - orchestration.

Wires the whole loop: scan -> council -> risk -> execution -> journal ->
shadow book, with the breadth-first adaptive ladder governing when quality
thresholds move.

The ladder is the anti-zero-trade mechanism and it has a deliberate order:

    11:00  expand the search
    12:30  relax quality (Tier 2)
    14:00  expand the search again
    14:15  relax quality (Tier 3)

Search wider before lowering standards. Hard safety gates never move at any
tier, and Tier 3 still enforces the liquidity floor, so the ladder can never
degrade into trading illiquid spreads to manufacture activity.

Every stage that stops a candidate writes a gate_rejections row, so a scan
that produces no trade is as measurable as one that does.

Place at: alpha_council/orchestrator.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Sequence

from alpha_council.agents.council import Council, CouncilOutcome, effective_risk_pct
from alpha_council.agents.evidence import EvidenceBuilder
from alpha_council.db.config_store import record_tier_change
from alpha_council.db.engine import Database
from alpha_council.execution.order_manager import OrderManager
from alpha_council.execution.position_monitor import MonitoredPosition, PositionMonitor
from alpha_council.journal.shadow_book import ShadowBook
from alpha_council.journal.trade_journal import RejectionLog, TradeJournal
from alpha_council.models.candidate import CandidateFeatures
from alpha_council.models.enums import (
    CandidateTrack,
    DataConfidence,
    DecisionState,
    Direction,
    GateStage,
    RiskDecision,
    ShadowVariant,
    Verdict,
)
from alpha_council.models.trading import OptionStructure
from alpha_council.risk.constitution import (
    PortfolioState,
    RiskConstitution,
    TradeRequest,
    sector_of,
)
from alpha_council.risk.position_sizing import size_position
from alpha_council.utils.ids import decision_id as make_decision_id
from alpha_council.utils.time import et_time_reached, to_et, utc_now


# ======================================================================
# tier ladder
# ======================================================================

@dataclass(slots=True)
class LadderState:
    tier: int = 1
    breadth_level: int = 0          # 0 core+normal, 1 expanded, 2 fully expanded
    orders_today: int = 0
    alpha_orders_total: int = 0
    pinned: bool = False
    session_date: str = ""
    changes: list[str] = field(default_factory=list)


class TierManager:
    """Owns the breadth-first ladder. Deterministic and fully testable."""

    def __init__(self, config: dict[str, Any], base_config_version: str):
        self.config = config
        self.base_config_version = base_config_version
        self.config_version = base_config_version
        b = config.get("breadth_expansion", {})
        self.first_expand = b.get("first_expand_et", "11:00")
        self.tier2_after = b.get("tier2_after_et", "12:30")
        self.second_expand = b.get("second_expand_et", "14:00")
        self.tier3_after = b.get("tier3_after_et", "14:15")
        self.pin_after = int(b.get("pin_tier1_after_alpha_orders", 14))
        self.state = LadderState()

    def start_session(self, now: datetime | None = None) -> None:
        now = now or utc_now()
        self.state.session_date = str(to_et(now).date())
        self.state.orders_today = 0
        self.state.breadth_level = 0
        self.state.changes = []
        # A new session resets quality to Tier 1. Yesterday's 2:15pm
        # desperation is not today's starting posture.
        if not self.state.pinned:
            self.state.tier = 1

    def note_order(self, is_alpha: bool = True) -> None:
        self.state.orders_today += 1
        if is_alpha:
            self.state.alpha_orders_total += 1
        if self.state.alpha_orders_total >= self.pin_after:
            self.state.pinned = True
            self.state.tier = 1

    @property
    def tier(self) -> int:
        return self.state.tier

    @property
    def breadth_level(self) -> int:
        return self.state.breadth_level

    def tier_config(self, tier: int | None = None) -> dict[str, Any]:
        return self.config.get("tiers", {}).get(tier or self.state.tier, {})

    def evaluate(self, now: datetime | None = None) -> list[str]:
        """Advance the ladder. Returns the transitions that occurred.

        Breadth always expands before quality relaxes, and nothing moves
        once an order has been submitted today.
        """
        now = now or utc_now()
        transitions: list[str] = []

        if self.state.pinned:
            return transitions
        if self.state.orders_today > 0:
            # An order today means the current settings are producing
            # trades. No further loosening, and no ratchet back down.
            return transitions

        if (self.state.breadth_level < 1
                and et_time_reached(self.first_expand, now)):
            self.state.breadth_level = 1
            transitions.append("BREADTH_EXPAND_1")

        if self.state.tier < 2 and et_time_reached(self.tier2_after, now):
            self.state.tier = 2
            transitions.append("TIER_2")

        if (self.state.breadth_level < 2
                and et_time_reached(self.second_expand, now)):
            self.state.breadth_level = 2
            transitions.append("BREADTH_EXPAND_2")

        if self.state.tier < 3 and et_time_reached(self.tier3_after, now):
            self.state.tier = 3
            transitions.append("TIER_3")

        self.state.changes.extend(transitions)
        return transitions

    def discovery_overrides(self) -> dict[str, Any]:
        """Breadth level maps onto discovery caps, not onto quality gates."""
        base = dict(self.config.get("discovery", {}))
        if self.state.breadth_level >= 1:
            base["max_dynamic_symbols"] = max(
                int(base.get("max_dynamic_symbols", 250)), 250)
            base["stage0_top_n"] = int(base.get("stage0_top_n", 30)) + 5
        if self.state.breadth_level >= 2:
            base["options_prescreen_top_n"] = int(
                base.get("options_prescreen_top_n", 12)) + 3
        return base

    async def persist_changes(self, db: Database, transitions: Sequence[str],
                              risk_config: dict[str, Any]) -> str:
        """A tier change is a configuration change and gets its own version.

        Without this, a trade taken at 14:22 under Tier 3 thresholds is
        indistinguishable from one taken at 09:45 under Tier 1.
        """
        if not transitions:
            return self.config_version
        self.config_version = await record_tier_change(
            db, self.base_config_version, self.state.tier, self.config,
            risk_config, reason=", ".join(transitions))
        return self.config_version


# ======================================================================
# pipeline
# ======================================================================

@dataclass(slots=True)
class DecisionOutcome:
    decision_id: str
    symbol: str
    traded: bool = False
    stage: str = ""
    gate_id: str | None = None
    reason: str = ""
    council: CouncilOutcome | None = None
    risk_decision: RiskDecision | None = None
    approved_qty: int = 0
    fill_debit: float | None = None
    cost_usd: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "traded": self.traded,
                "stage": self.stage, "gate": self.gate_id,
                "qty": self.approved_qty, "fill": self.fill_debit,
                "cost_usd": round(self.cost_usd, 4)}


class Orchestrator:
    """One candidate, end to end."""

    def __init__(self, db: Database, council: Council,
                 constitution: RiskConstitution, orders: OrderManager,
                 journal: TradeJournal, shadows: ShadowBook,
                 monitor: PositionMonitor, tiers: TierManager,
                 config: dict[str, Any], universe_config: dict[str, Any],
                 presubmit: Any = None):
        self.db = db
        self.council = council
        self.constitution = constitution
        self.orders = orders
        self.journal = journal
        self.shadows = shadows
        self.monitor = monitor
        self.tiers = tiers
        self.config = config
        self.universe_config = universe_config
        # §17.4: reprice from live quotes and re-run risk immediately
        # before submission. Optional so replay/tests without a market
        # connection keep working; production wiring always supplies it.
        self.presubmit = presubmit

    # ---- the decision path -------------------------------------------

    async def evaluate_candidate(
        self, candidate: CandidateFeatures, candidate_id: str,
        structures: Sequence[OptionStructure], builder: EvidenceBuilder,
        portfolio: PortfolioState, session_id: str,
        equity_confidence: DataConfidence = DataConfidence.HIGH,
        option_confidence: DataConfidence = DataConfidence.HIGH,
        rejections: RejectionLog | None = None,
        execute: bool = True, now: datetime | None = None,
    ) -> DecisionOutcome:
        now = now or utc_now()
        decision_id = make_decision_id()
        outcome = DecisionOutcome(decision_id=decision_id,
                                  symbol=candidate.symbol)

        await self.journal.open_decision(
            decision_id, candidate_id, candidate.symbol,
            self.tiers.config_version, str(candidate.discovery_source),
            candidate.track)

        # --- 1. council ---------------------------------------------
        # Stage markers land in system_events so a scheduled run that
        # stalls can be diagnosed after the fact. The scheduler logs only
        # on completion, which makes a slow stage and a hung one identical
        # from outside.
        await self.journal.transition(decision_id, DecisionState.COUNCIL_STARTED)
        await self.db.log_event(
            "INFO", "orchestrator", "STAGE_COUNCIL_START",
            f"{candidate.symbol} council starting",
            {"decision_id": decision_id, "structures": len(structures)})

        council = await self.council.run(candidate, structures, builder,
                                         decision_id, session_id)

        await self.db.log_event(
            "INFO", "orchestrator", "STAGE_COUNCIL_DONE",
            f"{candidate.symbol} {council.stopped_at}",
            {"decision_id": decision_id, "traded": council.traded,
             "calls": council.calls, "cost_usd": round(council.cost_usd, 4)})
        outcome.council = council
        outcome.cost_usd = council.cost_usd

        if council.proposal is not None:
            await self.journal.record_proposal(council.proposal, decision_id)
            await self.journal.transition(decision_id,
                                          DecisionState.PM_PROPOSED)
        await self.journal.record_structures(decision_id, structures,
                                             candidate_id)

        # The GPT original exists the moment the PM picks a structure,
        # regardless of what happens downstream. That is the baseline
        # every later effect is measured against.
        if council.original_structure is not None and council.proposal is not None:
            await self._create_gpt_original(decision_id, council, portfolio)

        if council.review is not None:
            proposal_id = f"prop_{decision_id[-8:]}_r0"
            await self.journal.record_review(decision_id, proposal_id,
                                             council.review)
            await self.journal.transition(decision_id,
                                          DecisionState.RED_TEAMED)
            await self._create_claude_variant(decision_id, council, portfolio)

        if council.revision is not None:
            await self.journal.record_proposal(council.revision, decision_id)

        if not council.traded:
            outcome.stage = council.stopped_at
            outcome.gate_id = council.gate_id
            outcome.reason = council.reason
            await self._log_rejection(rejections, candidate, decision_id,
                                      council)
            await self.journal.transition(decision_id,
                                          DecisionState.RISK_REJECTED,
                                          council.reason[:120])
            return outcome

        selected = council.selected_structure
        assert selected is not None
        await self.journal.transition(decision_id,
                                      DecisionState.STRUCTURE_SELECTED)

        # --- 2. risk constitution ------------------------------------
        # The sector map lives in risk_constitution.yaml; universe.yaml has
        # no `sectors` key, so reading it from there mapped every symbol to
        # UNKNOWN and neutered the sector cap.
        sector_map = (self.constitution.sectors
                      or self.universe_config.get("sectors", {}))
        request = TradeRequest(
            decision_id=decision_id, symbol=candidate.symbol,
            sector=sector_of(candidate.symbol, sector_map),
            direction=candidate.direction, structure=selected,
            desired_risk_pct=effective_risk_pct(council),
            pm_confidence=(council.final_proposal.confidence
                           if council.final_proposal else 0.0),
            # Original rev-0 conviction: the floor tests this; the Red
            # Team discount rides in via red_team_max_risk_pct below.
            pm_conviction=(council.proposal.confidence
                           if council.proposal else None),
            candidate_track=str(candidate.track),
            red_team_verdict=(council.review.verdict if council.review
                              else Verdict.PASS),
            # Claude's cap applies only when Claude asked for a change; a
            # PASS carries a recommendation the attribution cannot see.
            red_team_max_risk_pct=(
                council.review.recommended_max_risk_pct
                if council.review and council.review.verdict is Verdict.MODIFY
                else None),
            equity_data_confidence=equity_confidence,
            option_data_confidence=option_confidence,
            final_opportunity_score=candidate.final_opportunity_score,
            market_open=True,
        )
        evaluation = self.constitution.evaluate(
            request, portfolio, tier=self.tiers.tier,
            config_version=self.tiers.config_version, now=now)
        outcome.risk_decision = evaluation.decision
        outcome.approved_qty = evaluation.approved_qty

        await self.journal.record_risk(evaluation,
                                       f"prop_{decision_id[-8:]}_r0",
                                       selected.structure_id)

        if evaluation.decision.blocks_trade:
            outcome.stage = "RISK"
            outcome.gate_id = (evaluation.violations[0].rule_id
                               if evaluation.violations else "RISK_BLOCKED")
            outcome.reason = (evaluation.violations[0].message
                              if evaluation.violations else "risk blocked")
            if rejections is not None:
                rejections.add(candidate.symbol, GateStage.RISK,
                               outcome.gate_id, candidate.direction,
                               hard_gate=True, decision_id=decision_id,
                               structure=selected, note=outcome.reason[:200])
            await self.journal.transition(decision_id,
                                          DecisionState.RISK_REJECTED,
                                          outcome.reason[:120])
            return outcome

        await self.journal.transition(decision_id, DecisionState.RISK_APPROVED)

        if not execute:
            outcome.stage = "DRY_RUN"
            outcome.reason = "execution disabled"
            return outcome

        # --- 3. execution ---------------------------------------------
        # §17.4 pre-submit refresh: reprice from live quotes, then re-run
        # the Risk Constitution against the limit that will actually be
        # submitted. No stale approval may be reused.
        underlying_at_submit = selected.underlying_price or 0.0
        if self.presubmit is not None:
            refresh = await self.presubmit.refresh(
                selected, self.tiers.tier_config())
            if not refresh.ok:
                outcome.stage = "EXECUTION"
                outcome.gate_id = refresh.gate_id or "EXEC_STALE_PRESUBMIT"
                outcome.reason = refresh.reason
                if rejections is not None:
                    rejections.add(candidate.symbol, GateStage.EXECUTION,
                                   outcome.gate_id, candidate.direction,
                                   decision_id=decision_id,
                                   structure=selected,
                                   note=refresh.reason[:200])
                await self.journal.transition(decision_id,
                                              DecisionState.REJECTED,
                                              refresh.reason[:120])
                return outcome

            selected = refresh.structure or selected
            underlying_at_submit = refresh.underlying_price or underlying_at_submit
            request = replace(request, structure=selected)
            evaluation = self.constitution.evaluate(
                request, portfolio, tier=self.tiers.tier,
                config_version=self.tiers.config_version, now=utc_now())
            outcome.risk_decision = evaluation.decision
            outcome.approved_qty = evaluation.approved_qty
            await self.journal.record_risk(evaluation,
                                           f"prop_{decision_id[-8:]}_r0",
                                           selected.structure_id)
            if evaluation.decision.blocks_trade or evaluation.approved_qty < 1:
                outcome.stage = "RISK"
                outcome.gate_id = (evaluation.violations[0].rule_id
                                   if evaluation.violations
                                   else "RISK_PRESUBMIT_BLOCKED")
                outcome.reason = ("repriced structure failed re-approval: "
                                  + (evaluation.violations[0].message
                                     if evaluation.violations else ""))[:200]
                if rejections is not None:
                    rejections.add(candidate.symbol, GateStage.RISK,
                                   outcome.gate_id, candidate.direction,
                                   hard_gate=True, decision_id=decision_id,
                                   structure=selected,
                                   note=outcome.reason)
                await self.journal.transition(decision_id,
                                              DecisionState.RISK_REJECTED,
                                              outcome.reason[:120])
                return outcome
            # The audit row must show the prices that were submitted.
            await self.journal.record_structures(decision_id, [selected],
                                                 candidate_id)

        await self.db.log_event(
            "INFO", "orchestrator", "STAGE_SUBMITTING",
            f"{candidate.symbol} qty {evaluation.approved_qty}",
            {"decision_id": decision_id})
        await self.journal.transition(decision_id,
                                      DecisionState.ORDER_SUBMITTED)
        # Walk ceiling: the granted risk BUDGET per spread, not the initial
        # limit. approved_max_loss/qty equals the initial limit debit by
        # construction, and a ceiling equal to the first rung means the
        # walk's second and third attempts are clipped out of existence.
        budget = evaluation.approved_risk_budget or evaluation.approved_max_loss
        max_debit = budget / max(1, evaluation.approved_qty) / 100.0
        max_debit = max(max_debit, selected.initial_limit_debit)
        execution = await self.orders.execute_with_walk(
            selected, decision_id, evaluation.approved_qty, max_debit)

        if not execution.filled or execution.fill_debit is None:
            outcome.stage = "EXECUTION"
            outcome.gate_id = "EXEC_NO_FILL"
            outcome.reason = execution.final_status
            if rejections is not None:
                rejections.add(candidate.symbol, GateStage.EXECUTION,
                               "EXEC_NO_FILL", candidate.direction,
                               decision_id=decision_id, structure=selected,
                               note=f"{execution.walk_steps} attempts")
            await self.journal.transition(decision_id, DecisionState.NO_FILL)
            return outcome

        # --- 4. journal, shadow, monitor ------------------------------
        outcome.traded = True
        outcome.stage = "FILLED"
        outcome.fill_debit = execution.fill_debit
        await self.journal.transition(decision_id, DecisionState.FILLED)

        await self.journal.open_trade(
            decision_id, candidate.symbol, evaluation.approved_qty,
            execution.fill_debit,
            thesis=(council.final_proposal.thesis
                    if council.final_proposal else ""),
            invalidation=[r.model_dump() for r in
                          (council.final_proposal.invalidation
                           if council.final_proposal else [])],
            track=candidate.track, opened_at=execution.filled_at or now)

        await self.shadows.create(
            decision_id, ShadowVariant.EXECUTED, selected,
            evaluation.approved_qty, entry_debit=execution.fill_debit,
            entry_timestamp=execution.filled_at or now)

        self.monitor.track(MonitoredPosition(
            decision_id=decision_id, symbol=candidate.symbol,
            structure=selected, qty=evaluation.approved_qty,
            entry_debit=execution.fill_debit,
            opened_at=execution.filled_at or now,
            invalidation=list(council.final_proposal.invalidation)
            if council.final_proposal else [],
            horizon_days=(council.final_proposal.expected_horizon_days
                          if council.final_proposal else 5),
            track=candidate.track))

        self.tiers.note_order(
            is_alpha=candidate.track is not CandidateTrack.CALIBRATION)

        await self.orders.record_calibration(
            execution, selected, candidate.track, candidate.direction,
            underlying_at_submit=underlying_at_submit
            or selected.underlying_price or 0.01)
        return outcome

    # ---- shadow variants ---------------------------------------------

    async def _create_gpt_original(self, decision_id: str,
                                   council: CouncilOutcome,
                                   portfolio: PortfolioState) -> None:
        """Size at what the PM actually asked for.

        Capped only by the account value, per spec §19.1. Applying the risk
        limits here would make the baseline the risk engine's decision
        rather than the PM's, and the whole point is to measure the
        difference between them.
        """
        proposal = council.proposal
        structure = council.original_structure
        if proposal is None or structure is None:
            return
        sizing = size_position(
            equity=portfolio.equity,
            desired_risk_pct=proposal.desired_portfolio_risk_pct,
            max_loss_per_spread=structure.max_loss_per_spread,
            hard_cap_pct=100.0)
        await self.shadows.create(
            decision_id, ShadowVariant.GPT_ORIGINAL, structure,
            max(0, sizing.requested_qty))

    async def _create_claude_variant(self, decision_id: str,
                                     council: CouncilOutcome,
                                     portfolio: PortfolioState) -> None:
        review = council.review
        if review is None:
            return

        # A VETO produces a flat variant, not a missing one. The value of a
        # trade avoided is measurable only if the variant exists.
        if review.verdict is Verdict.VETO:
            structure = council.original_structure
            if structure is not None:
                await self.shadows.create(
                    decision_id, ShadowVariant.CLAUDE_MODIFIED, structure,
                    qty=0)
            return

        if review.verdict is Verdict.PASS:
            return          # nothing changed; compute() falls back to GPT

        structure = council.selected_structure
        proposal = council.final_proposal
        if structure is None or proposal is None:
            return
        sizing = size_position(
            equity=portfolio.equity,
            desired_risk_pct=proposal.desired_portfolio_risk_pct,
            max_loss_per_spread=structure.max_loss_per_spread,
            hard_cap_pct=100.0)
        await self.shadows.create(
            decision_id, ShadowVariant.CLAUDE_MODIFIED, structure,
            max(0, sizing.requested_qty))

    async def _log_rejection(self, rejections: RejectionLog | None,
                             candidate: CandidateFeatures, decision_id: str,
                             council: CouncilOutcome) -> None:
        if rejections is None or council.gate_id is None:
            return
        stage = {
            "PM_ABSTAIN": GateStage.PM_ABSTAIN,
            "RED_TEAM": GateStage.RED_TEAM,
            "REVISION": GateStage.RED_TEAM,
        }.get(council.stopped_at, GateStage.BUDGET
              if council.gate_id == "BUDGET_EXHAUSTED"
              else GateStage.OPPORTUNITY_SCORE)

        rejections.add(
            candidate.symbol, stage, council.gate_id, candidate.direction,
            decision_id=decision_id,
            structure=council.selected_structure or council.original_structure,
            note=council.reason[:200])


# ======================================================================
# session
# ======================================================================

@dataclass(slots=True)
class SessionSummary:
    session_id: str
    started_at: datetime
    scans: int = 0
    candidates_evaluated: int = 0
    councils_run: int = 0
    trades_opened: int = 0
    cost_usd: float = 0.0
    tier_changes: list[str] = field(default_factory=list)
    outcomes: list[DecisionOutcome] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        by_gate: dict[str, int] = {}
        for o in self.outcomes:
            if o.gate_id:
                by_gate[o.gate_id] = by_gate.get(o.gate_id, 0) + 1
        return {
            "session_id": self.session_id,
            "scans": self.scans,
            "candidates_evaluated": self.candidates_evaluated,
            "councils_run": self.councils_run,
            "trades_opened": self.trades_opened,
            "cost_usd": round(self.cost_usd, 4),
            "tier_changes": self.tier_changes,
            "stopped_by": dict(sorted(by_gate.items(),
                                      key=lambda kv: -kv[1])),
        }
