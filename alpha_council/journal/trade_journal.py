"""
Alpha Council v2.5 - trade journal and gate rejection log.

Two records, one purpose: make every decision and every non-decision
reconstructable after the fact.

The rejection log is not error logging. It is the measurement instrument
behind the Gate Lab: every gate that stops a candidate writes exactly one
row, carrying the config version in force, so a gate's value can be
computed rather than assumed.

Place at: alpha_council/journal/trade_journal.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from alpha_council.db.engine import Database
from alpha_council.models.enums import (
    CandidateTrack,
    DecisionState,
    Direction,
    ExitReason,
    GateStage,
)
from alpha_council.models.risk import GateRejection
from alpha_council.models.trading import OptionStructure, PortfolioProposal
from alpha_council.utils.ids import new_uuid, rejection_id
from alpha_council.utils.time import iso_utc, utc_now


@dataclass(slots=True)
class TradeRecord:
    trade_id: str
    decision_id: str
    symbol: str
    status: str
    qty: int = 0
    entry_debit: float | None = None
    exit_credit: float | None = None
    realized_pnl: float | None = None
    realized_return_pct: float | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    exit_reason: str | None = None
    track: CandidateTrack = CandidateTrack.MOMENTUM

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None and self.closed_at is None

    @property
    def holding_seconds(self) -> float | None:
        if not (self.opened_at and self.closed_at):
            return None
        return (self.closed_at - self.opened_at).total_seconds()


class TradeJournal:
    """Owns decision state transitions and the trade lifecycle record."""

    def __init__(self, db: Database):
        self.db = db

    # ---- decisions --------------------------------------------------

    async def open_decision(self, decision_id: str, candidate_id: str,
                            symbol: str, config_version: str,
                            discovery_source: str, track: CandidateTrack
                            ) -> None:
        now = iso_utc()
        await self.db.execute(
            "INSERT OR IGNORE INTO decisions(decision_id, candidate_id, "
            "config_version, symbol, state, created_at, updated_at, "
            "discovery_source, candidate_track) VALUES(?,?,?,?,?,?,?,?,?)",
            (decision_id, candidate_id, config_version, symbol,
             str(DecisionState.CANDIDATE), now, now, discovery_source,
             str(track)))

    async def transition(self, decision_id: str, state: DecisionState,
                         note: str = "") -> None:
        """Every state change is persisted, so a crash leaves the database
        showing the last completed step rather than an ambiguous middle."""
        await self.db.execute(
            "UPDATE decisions SET state=?, updated_at=? WHERE decision_id=?",
            (str(state), iso_utc(), decision_id))
        await self.db.log_event("INFO", "trade_journal", "STATE_TRANSITION",
                                f"{decision_id} -> {state}",
                                {"state": str(state), "note": note},
                                decision_id=decision_id)

    async def state_of(self, decision_id: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT state FROM decisions WHERE decision_id=?", (decision_id,))
        return row["state"] if row else None

    # ---- proposals and reviews ---------------------------------------

    async def record_proposal(self, proposal: PortfolioProposal) -> str:
        proposal_id = f"prop_{proposal.decision_id[-8:]}_r{proposal.revision}"
        await self.db.execute(
            "INSERT OR REPLACE INTO trade_proposals(proposal_id, decision_id, "
            "revision, symbol, trade, direction, confidence, "
            "expected_horizon_days, desired_portfolio_risk_pct, thesis, "
            "catalyst_summary, key_supporting_evidence_json, "
            "key_contrary_evidence_json, invalidation_json, "
            "selected_structure_rank, abstain_reason, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (proposal_id, proposal.decision_id, proposal.revision,
             proposal.symbol, 1 if proposal.trade else 0,
             str(proposal.direction), proposal.confidence,
             proposal.expected_horizon_days,
             proposal.desired_portfolio_risk_pct, proposal.thesis,
             proposal.catalyst_summary,
             json.dumps(proposal.key_supporting_evidence),
             json.dumps(proposal.key_contrary_evidence),
             json.dumps([r.model_dump() for r in proposal.invalidation]),
             proposal.selected_structure_rank, proposal.abstain_reason,
             iso_utc()))
        return proposal_id

    async def record_structures(self, decision_id: str,
                                structures: Sequence[OptionStructure],
                                candidate_id: str | None = None) -> None:
        rows = []
        for s in structures:
            rows.append((
                s.structure_id, decision_id, candidate_id, s.rank, s.symbol,
                str(s.strategy), s.expiration.isoformat(), s.dte,
                s.long_leg.symbol, s.long_leg.strike, s.long_leg.delta,
                s.long_leg.bid, s.long_leg.ask, s.long_leg.raw_mid,
                s.long_leg.adjusted_mid,
                s.short_leg.symbol, s.short_leg.strike, s.short_leg.delta,
                s.short_leg.bid, s.short_leg.ask, s.short_leg.raw_mid,
                s.short_leg.adjusted_mid,
                s.net_delta, s.width, s.raw_mid_debit, s.adjusted_mid_debit,
                s.natural_debit, s.staleness_buffer, s.initial_limit_debit,
                s.cost_to_width_ratio, s.max_loss_per_spread,
                s.max_profit_per_spread, s.reward_risk_ratio, s.breakeven,
                s.max_quote_lag_seconds, s.underlying_price, s.underlying_move,
                1 if s.stale_adjusted else 0, s.liquidity_score,
                s.delta_fit_score, s.dte_fit_score, s.cost_efficiency_score,
                s.structure_score, s.staleness_buffer,
                s.model_dump_json()[:20000], iso_utc()))
        await self.db.executemany(
            "INSERT OR REPLACE INTO option_structures("
            "structure_id, decision_id, candidate_id, rank, symbol, strategy, "
            "expiration, dte, long_symbol, long_strike, long_delta, long_bid, "
            "long_ask, long_raw_mid, long_adjusted_mid, short_symbol, "
            "short_strike, short_delta, short_bid, short_ask, short_raw_mid, "
            "short_adjusted_mid, net_delta, width, raw_mid_debit, "
            "adjusted_mid_debit, natural_debit, staleness_buffer, "
            "initial_limit_debit, cost_to_width_ratio, max_loss_per_spread, "
            "max_profit_per_spread, reward_risk_ratio, breakeven, "
            "max_quote_lag_seconds, underlying_price, underlying_move, "
            "stale_adjusted, liquidity_score, delta_fit_score, dte_fit_score, "
            "cost_efficiency_score, structure_score, indicative_buffer, "
            "raw_json, created_at) VALUES("
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    async def record_review(self, decision_id: str, proposal_id: str,
                            review: Any) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO red_team_reviews(review_id, decision_id, "
            "proposal_id, verdict, risk_score, fatal_flaw, "
            "confidence_adjustment, recommended_max_risk_pct, problems_json, "
            "strongest_counterargument, information_to_reverse_json, summary, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"rev_{decision_id[-8:]}", decision_id, proposal_id,
             str(review.verdict), review.risk_score,
             1 if review.fatal_flaw else 0, review.confidence_adjustment,
             review.recommended_max_risk_pct,
             json.dumps([p.model_dump() for p in review.problems]),
             review.strongest_counterargument,
             json.dumps(review.information_to_reverse_verdict),
             review.summary, iso_utc()))

    async def record_risk(self, evaluation: Any, proposal_id: str,
                          structure_id: str) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO risk_evaluations(risk_evaluation_id, "
            "decision_id, proposal_id, structure_id, config_version, "
            "evaluated_at, decision, account_equity, requested_qty, "
            "approved_qty, requested_max_loss, approved_max_loss, "
            "total_open_risk_pct_after, sector_risk_pct_after, "
            "daily_drawdown_pct, competition_drawdown_pct, violations_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"risk_{evaluation.decision_id[-8:]}", evaluation.decision_id,
             proposal_id, structure_id, evaluation.config_version,
             iso_utc(evaluation.evaluated_at), str(evaluation.decision),
             evaluation.account_equity, evaluation.requested_qty,
             evaluation.approved_qty, evaluation.requested_max_loss,
             evaluation.approved_max_loss,
             evaluation.total_open_risk_pct_after,
             evaluation.sector_risk_pct_after, evaluation.daily_drawdown_pct,
             evaluation.competition_drawdown_pct,
             json.dumps([v.model_dump() for v in evaluation.violations])))

    # ---- trade lifecycle ---------------------------------------------

    async def open_trade(self, decision_id: str, symbol: str, qty: int,
                         entry_debit: float, thesis: str,
                         invalidation: list[dict[str, Any]],
                         track: CandidateTrack,
                         opened_at: datetime | None = None) -> TradeRecord:
        record = TradeRecord(
            trade_id=f"trd_{decision_id[-8:]}", decision_id=decision_id,
            symbol=symbol, status="OPEN", qty=qty, entry_debit=entry_debit,
            opened_at=opened_at or utc_now(), track=track)
        await self.db.execute(
            "INSERT OR REPLACE INTO trade_journal(trade_id, decision_id, "
            "opened_at, status, qty, entry_debit, thesis, invalidation_json, "
            "candidate_track) VALUES(?,?,?,?,?,?,?,?,?)",
            (record.trade_id, decision_id, iso_utc(record.opened_at),
             "OPEN", qty, entry_debit, thesis, json.dumps(invalidation),
             str(track)))
        await self.transition(decision_id, DecisionState.POSITION_OPEN)
        return record

    async def close_trade(self, decision_id: str, exit_credit: float,
                          reason: ExitReason,
                          closed_at: datetime | None = None,
                          lesson: str | None = None) -> TradeRecord | None:
        row = await self.db.fetchone(
            "SELECT * FROM trade_journal WHERE decision_id=?", (decision_id,))
        if not row:
            return None

        qty = int(row["qty"] or 0)
        entry = float(row["entry_debit"] or 0.0)
        # A debit spread is closed for a credit; profit is the difference,
        # times 100 per contract, times quantity.
        realized = round((exit_credit - entry) * 100 * qty, 2)
        return_pct = (round(realized / (entry * 100 * qty) * 100, 4)
                      if entry > 0 and qty > 0 else 0.0)
        closed = closed_at or utc_now()

        await self.db.execute(
            "UPDATE trade_journal SET closed_at=?, status=?, exit_credit=?, "
            "realized_pnl=?, realized_return_pct=?, exit_reason=?, lesson=? "
            "WHERE decision_id=?",
            (iso_utc(closed), "CLOSED", exit_credit, realized, return_pct,
             str(reason), lesson, decision_id))
        await self.transition(decision_id, DecisionState.POSITION_CLOSED,
                              f"{reason} pnl={realized:+.2f}")

        return TradeRecord(
            trade_id=row["trade_id"], decision_id=decision_id,
            symbol=row.get("symbol", ""), status="CLOSED", qty=qty,
            entry_debit=entry, exit_credit=exit_credit,
            realized_pnl=realized, realized_return_pct=return_pct,
            closed_at=closed, exit_reason=str(reason))

    async def open_trades(self) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT * FROM trade_journal WHERE status='OPEN' "
            "ORDER BY opened_at")

    async def performance(self) -> dict[str, Any]:
        """Deterministic performance summary. No model ever computes these."""
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n, "
            "SUM(realized_pnl) AS total, "
            "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins, "
            "AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END) AS avg_win, "
            "AVG(CASE WHEN realized_pnl <= 0 THEN realized_pnl END) AS avg_loss "
            "FROM trade_journal WHERE status='CLOSED'")
        n = int((row or {}).get("n") or 0)
        if n == 0:
            return {"closed_trades": 0, "total_pnl": 0.0, "win_rate": None,
                    "expectancy": None, "profit_factor": None}

        wins = int(row.get("wins") or 0)
        win_rate = wins / n
        avg_win = float(row.get("avg_win") or 0.0)
        avg_loss = float(row.get("avg_loss") or 0.0)

        gross = await self.db.fetchone(
            "SELECT SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) "
            "AS profit, SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 "
            "END) AS loss FROM trade_journal WHERE status='CLOSED'")
        gross_loss = abs(float((gross or {}).get("loss") or 0.0))

        return {
            "closed_trades": n,
            "total_pnl": round(float(row.get("total") or 0.0), 2),
            "win_rate": round(win_rate, 4),
            "average_win": round(avg_win, 2),
            "average_loss": round(avg_loss, 2),
            "expectancy": round(win_rate * avg_win + (1 - win_rate) * avg_loss, 2),
            # NULL rather than infinity when nothing has lost yet.
            "profit_factor": (round(float((gross or {}).get("profit") or 0.0)
                                    / gross_loss, 3)
                              if gross_loss > 0 else None),
        }


class RejectionLog:
    """Single writer for gate_rejections, so no stage invents its own shape."""

    def __init__(self, db: Database, config_version: str, tier: int = 1):
        self.db = db
        self.config_version = config_version
        self.tier = tier
        self.buffer: list[GateRejection] = []

    def add(self, symbol: str, stage: GateStage, gate_id: str,
            direction: Direction = Direction.NEUTRAL,
            observed: Any = None, threshold: Any = None,
            hard_gate: bool = False, scan_id: str | None = None,
            decision_id: str | None = None,
            structure: OptionStructure | None = None,
            note: str = "") -> GateRejection:
        shadow_eligible = bool(structure is not None and stage.shadow_eligible)
        rejection = GateRejection(
            rejection_id=rejection_id(), occurred_at=utc_now(),
            config_version=self.config_version, scan_id=scan_id,
            decision_id=decision_id, symbol=symbol, direction=direction,
            stage=stage, gate_id=gate_id,
            observed_value=str(observed) if observed is not None else None,
            threshold_value=str(threshold) if threshold is not None else None,
            tier=self.tier, hard_gate=hard_gate,
            shadow_eligible=shadow_eligible,
            shadow_structure_json=(structure.model_dump_json()
                                   if shadow_eligible else None),
            note=note)
        self.buffer.append(rejection)
        return rejection

    async def flush(self) -> int:
        if not self.buffer:
            return 0
        rows = [(r.rejection_id, iso_utc(r.occurred_at), r.config_version,
                 r.scan_id, r.decision_id, r.symbol, str(r.direction),
                 str(r.stage), r.gate_id, r.observed_value, r.threshold_value,
                 r.tier, 1 if r.hard_gate else 0,
                 1 if r.shadow_eligible else 0, r.shadow_structure_json,
                 r.note) for r in self.buffer]
        await self.db.executemany(
            "INSERT OR IGNORE INTO gate_rejections(rejection_id, occurred_at, "
            "config_version, scan_id, decision_id, symbol, direction, stage, "
            "gate_id, observed_value, threshold_value, tier, hard_gate, "
            "shadow_eligible, shadow_structure_json, note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        count = len(rows)
        self.buffer.clear()
        return count

    async def histogram(self) -> list[dict[str, Any]]:
        return await self.db.fetchall("SELECT * FROM v_gate_histogram")

    async def gate_value(self) -> list[dict[str, Any]]:
        """GateValue(g) = -1 * mean hypothetical P&L of what g blocked.

        Positive means the gate earned its place. Persistently negative
        means it is systematically blocking profitable trades.
        """
        return await self.db.fetchall("SELECT * FROM v_gate_value")
