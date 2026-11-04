"""
Alpha Council v2.7 - the council-driven stock sleeve (operator
rebalance, 2026-10-30; "route one": reuse the council for share
proposals).

The same funnel, the same independent analysts, the same Red Team - but
the expression is LONG SHARES instead of a debit vertical, so finalists
the options engine could not serve (thin chains, liquidity gates) and
finalists the options seats never reached can still carry a thesis.

What stays constitutional:
  LONG-ONLY. Shares have no defined-risk short expression on this desk.
  THE STOP IS THE PM'S OWN INVALIDATION. A proposal without a usable
  PRICE stop below entry is refused deterministically - no stop, no
  shares. Position size derives from stop distance (risk-based), then a
  notional cap, then sleeve headroom; the smallest number wins.
  A VETO IS FINAL, and a MODIFY's recommended risk is applied directly
  as a cap (no revision round: the Red Team is a brake, applied, not
  negotiated with a second LLM call).
  SOFT STOPS ARE SOFT. A stop breach must hold for a confirmation
  window before the sell fires (the DRAM lesson), and gap risk through
  a stop is bounded by the notional cap, not pretended away.

Everything is journaled through the same decisions / trade_proposals /
red_team_reviews tables as options councils; the position ledger is
sleeve_trades, and every order lands in sleeve_orders so the sleeve
isolation identity in accounting.py holds.

Place at: alpha_council/sleeves/stock_sleeve.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from alpha_council.agents.council import MIN_ANALYSTS, Council, CouncilOutcome
from alpha_council.db.engine import Database
from alpha_council.models.candidate import CandidateFeatures
from alpha_council.models.enums import DecisionState, Direction, GateStage, Verdict
from alpha_council.models.trading import InvalidationRule, PortfolioProposal, RedTeamReview
from alpha_council.sleeves.accounting import read_accounting, record_snapshots
from alpha_council.sleeves.equity_exec import submit_marketable
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.time import is_rth, iso_utc, utc_now


# ----------------------------------------------------------------------
# pure sizing and stop extraction - testable without a broker
# ----------------------------------------------------------------------

def usable_stop(invalidation: Sequence[InvalidationRule], entry: float,
                min_distance_pct: float = 1.0,
                max_distance_pct: float = 12.0) -> float | None:
    """The PM's own PRICE stop below entry, or None (= refuse the trade).

    Long-only, so the stop must sit BELOW entry with a downward
    comparator. Closer than min_distance is noise that churns; farther
    than max_distance is a thesis, not a stop. The tightest qualifying
    rule wins because it is the PM's binding one.
    """
    stops: list[float] = []
    for rule in invalidation:
        if rule.rule_type != "PRICE" or rule.threshold is None:
            continue
        if rule.comparator not in ("LT", "LTE"):
            continue
        distance_pct = (entry - rule.threshold) / entry * 100.0
        if min_distance_pct <= distance_pct <= max_distance_pct:
            stops.append(float(rule.threshold))
    return max(stops) if stops else None


def size_shares(equity: float, entry: float, stop: float,
                requested_risk_pct: float, risk_cap_pct: float,
                max_position_pct: float, headroom: float) -> int:
    """Risk-based share count; every cap composes and the smallest wins."""
    per_share_risk = entry - stop
    if per_share_risk <= 0 or entry <= 0:
        return 0
    risk_budget = min(requested_risk_pct, risk_cap_pct) / 100.0 * equity
    qty_by_risk = int(risk_budget / per_share_risk)
    notional_cap = min(max_position_pct / 100.0 * equity, max(0.0, headroom))
    qty_by_notional = int(notional_cap / entry)
    return max(0, min(qty_by_risk, qty_by_notional))


@dataclass(slots=True)
class StockPosition:
    trade_id: str
    decision_id: str
    symbol: str
    qty: int
    entry_price: float
    stop_price: float
    target_price: float | None
    horizon_days: int
    opened_at: datetime
    breach_since: datetime | None = None


def evaluate_stock_exit(position: StockPosition, price: float,
                        now: datetime, *, stop_confirm_minutes: float,
                        horizon_multiple: float) -> tuple[str | None, str]:
    """(exit_reason, detail). Pure; the caller owns I/O and RTH gating.

    The stop needs a continuous confirmation window (position.breach_since
    carries first-breach time between polls; the caller persists it); the
    target and the time stop fire immediately.
    """
    if position.target_price and price >= position.target_price:
        return ("UNDERLYING_TARGET",
                f"{price:.2f} reached target {position.target_price:.2f}")

    held_days = (now - position.opened_at).total_seconds() / 86400.0
    if held_days >= position.horizon_days * horizon_multiple:
        return ("TIME_STOP",
                f"held {held_days:.1f}d vs {position.horizon_days}d horizon "
                f"x{horizon_multiple}")

    if price <= position.stop_price:
        since = position.breach_since or now
        position.breach_since = since
        held_minutes = (now - since).total_seconds() / 60.0
        if held_minutes >= stop_confirm_minutes:
            return ("UNDERLYING_INVALIDATION",
                    f"{price:.2f} below stop {position.stop_price:.2f} "
                    f"held {held_minutes:.0f}m")
        return (None, f"stop breached, confirming ({held_minutes:.0f}m)")
    position.breach_since = None
    return (None, "")


# ----------------------------------------------------------------------
# service
# ----------------------------------------------------------------------

class StockSleeve:
    def __init__(self, council: Council | None, orders: Any, market: Any,
                 journal: Any, db: Database, scoring: dict[str, Any]):
        cfg = scoring.get("sleeves", {}).get("stock", {})
        self.council = council
        self.orders = orders
        self.market = market
        self.journal = journal
        self.db = db
        self.config = scoring
        self.enabled = bool(cfg.get("enabled", False))
        self.target_pct = float(cfg.get("target_pct_of_equity", 30.0))
        self.long_only = bool(cfg.get("long_only", True))
        self.max_positions = int(cfg.get("max_positions", 6))
        self.max_position_pct = float(cfg.get("max_position_pct_of_equity", 6.0))
        self.risk_cap_pct = float(cfg.get("risk_pct_of_equity_per_trade", 0.75))
        self.min_stop_pct = float(cfg.get("min_stop_distance_pct", 1.0))
        self.max_stop_pct = float(cfg.get("max_stop_distance_pct", 12.0))
        self.councils_per_scan = int(cfg.get("councils_per_scan", 1))
        self.horizon_multiple = float(cfg.get("time_stop_horizon_multiple", 1.5))
        self.target_r = float(cfg.get("profit_target_r_multiple", 2.0))
        self.stop_confirm_minutes = float(cfg.get("stop_confirm_minutes", 10.0))
        self.excluded: set[str] = set()     # ETF cohort etc., set by wiring
        self.tracked: list[StockPosition] = []
        self._exit_awaiting_rth: set[str] = set()
        self._pm_prompt: str | None = None
        self._rt_prompt: str | None = None

    def _prompt(self, which: str) -> str:
        from alpha_council.settings import load_prompt

        if which == "pm":
            if self._pm_prompt is None:
                self._pm_prompt = load_prompt("pm_equity_system")
            return self._pm_prompt
        if self._rt_prompt is None:
            self._rt_prompt = load_prompt("red_team_equity_system")
        return self._rt_prompt

    # ---- restore / accounting ---------------------------------------

    async def restore(self) -> int:
        rows = await self.db.fetchall(
            "SELECT * FROM sleeve_trades WHERE status='OPEN'")
        self.tracked = [StockPosition(
            trade_id=r["trade_id"], decision_id=r["decision_id"] or "",
            symbol=r["symbol"], qty=int(r["qty"]),
            entry_price=float(r["entry_price"]),
            stop_price=float(r["stop_price"]),
            target_price=(float(r["target_price"])
                          if r["target_price"] is not None else None),
            horizon_days=int(r["horizon_days"]),
            opened_at=datetime.fromisoformat(r["opened_at"]),
        ) for r in rows]
        return len(self.tracked)

    def held_symbols(self) -> set[str]:
        return {p.symbol for p in self.tracked}

    async def current_notional(self) -> float:
        state = await read_accounting(self.orders, self.db,
                                      self.held_symbols())
        return state.sleeve_market_value

    # ---- expression --------------------------------------------------

    async def consider(self, candidates: Sequence[CandidateFeatures],
                       candidate_ids: dict[str, str], equity: float,
                       builder_factory: Any, session_id: str,
                       config_version: str, rejections: Any = None,
                       execute: bool = True) -> int:
        """Run equity councils for up to councils_per_scan candidates.

        ``candidates`` must already exclude symbols seated at options
        councils this scan; this method applies the sleeve's own gates on
        top. ``builder_factory(candidate)`` is awaited and must return an
        EvidenceBuilder.
        """
        if not (self.enabled and self.council is not None):
            return 0
        opened = 0
        current_mv = await self.current_notional()
        headroom = self.target_pct / 100.0 * equity - current_mv

        councils = 0
        skipped_held = 0
        skipped_direction = 0
        skipped_cooldown = 0
        stopped = "exhausted"  # looked at every candidate offered
        for candidate in candidates:
            if opened >= self.councils_per_scan:
                stopped = "councils_per_scan"
                break
            if len(self.tracked) >= self.max_positions:
                stopped = "max_positions"
                break
            if headroom < 500.0:
                stopped = "headroom"
                break
            symbol = candidate.symbol.upper()
            if symbol in self.excluded or symbol in self.held_symbols():
                skipped_held += 1
                continue
            if self.long_only and candidate.direction is not Direction.BULLISH:
                skipped_direction += 1
                continue
            if await self._abstained_recently(candidate):
                skipped_cooldown += 1
                await self.db.log_event(
                    "INFO", "stock_sleeve", "STOCK_SKIP_RECENT_ABSTAIN",
                    f"{symbol}: PM abstained within the cooldown on "
                    "materially unchanged evidence; no new council")
                continue

            councils += 1
            builder = await builder_factory(candidate)
            filled_notional = await self._equity_council(
                candidate, candidate_ids.get(candidate.symbol, ""),
                builder, session_id, config_version,
                equity, headroom, rejections, execute)
            if filled_notional:
                opened += 1
                headroom -= filled_notional

        # One line per scan, even (especially) when nothing opened: an
        # all-decline pass is otherwise indistinguishable from dead wiring.
        await self.db.log_event(
            "INFO", "stock_sleeve", "STOCK_SLEEVE_SCAN",
            f"offered {len(candidates)}: {skipped_direction} non-bullish, "
            f"{skipped_held} excluded/held, {skipped_cooldown} cooldown, "
            f"{councils} council(s), {opened} opened ({stopped})",
            {"headroom_left": round(headroom, 2),
             "sizing_equity": round(equity, 2)})
        return opened

    async def _abstained_recently(self, candidate: CandidateFeatures) -> bool:
        """The options seat allocator's abstain cooldown, applied here.

        A PM abstain on the symbol within the cooldown window does not
        earn another council unless the candidate's score has moved by
        the rescore delta - without this the sleeve re-hears the same
        leftover every scan (CIFR: five identical abstentions,
        2026-11-03, the PM itself citing "no material change").
        """
        cs = self.config.get("council_seats", {})
        cooldown_h = float(cs.get("abstain_cooldown_hours", 2.0))
        rescore = float(cs.get("abstain_rescore_delta", 4.0))
        if cooldown_h <= 0:
            return False
        cutoff = (utc_now() - timedelta(hours=cooldown_h)).isoformat()
        recent = await self.db.fetchone(
            "SELECT s.final_opportunity_score AS score "
            "FROM trade_proposals p "
            "JOIN decisions d ON d.decision_id = p.decision_id "
            "LEFT JOIN candidate_scores s ON s.candidate_id = d.candidate_id "
            "WHERE d.symbol = ? AND p.trade = 0 AND p.created_at >= ? "
            "ORDER BY p.created_at DESC LIMIT 1",
            (candidate.symbol, cutoff))
        if recent is None:
            return False
        then = recent.get("score")
        return (then is not None
                and abs(candidate.final_opportunity_score - float(then))
                < rescore)

    async def _equity_council(self, candidate: CandidateFeatures,
                              candidate_id: str, builder: Any,
                              session_id: str, config_version: str,
                              equity: float, headroom: float,
                              rejections: Any, execute: bool) -> float:
        """One candidate through analysts -> PM -> Red Team -> shares.

        Returns the filled notional (0.0 on any refusal), so the caller
        can decrement headroom without re-reading the broker.
        """
        from alpha_council.utils.ids import decision_id as make_decision_id

        decision_id = make_decision_id()
        await self.journal.open_decision(
            decision_id, candidate_id, candidate.symbol, config_version,
            str(candidate.discovery_source), candidate.track)
        await self.journal.transition(decision_id,
                                      DecisionState.COUNCIL_STARTED,
                                      "stock sleeve")
        outcome = CouncilOutcome(decision_id=decision_id,
                                 symbol=candidate.symbol)

        def refuse(stage: GateStage, gate_id: str, note: str) -> float:
            if rejections is not None:
                rejections.add(candidate.symbol, stage, gate_id,
                               candidate.direction, decision_id=decision_id,
                               note=note[:200])
            return 0.0

        assessments = await self.council.run_analysts(
            builder, decision_id, session_id, outcome)
        if len(assessments) < MIN_ANALYSTS:
            await self.journal.transition(decision_id,
                                          DecisionState.RISK_REJECTED,
                                          "stock: analysts failed")
            return refuse(GateStage.OPPORTUNITY_SCORE,
                          "STOCK_ANALYSTS_FAILED",
                          f"only {len(assessments)}/3 analysts")

        # PM, equity flavor: same purpose (model + budget line), its own
        # system prompt. selected_structure_rank stays null for shares.
        pm_pkg = builder.build("PM", cap_tokens=6000,
                               analyst_outputs=assessments)
        result = await self.council.openai.call(
            "portfolio_manager", self._prompt("pm"), pm_pkg,
            PortfolioProposal, decision_id=decision_id,
            session_id=session_id, estimated_cost=0.05)
        if result.failed or not isinstance(result.parsed, PortfolioProposal):
            await self.journal.transition(decision_id,
                                          DecisionState.RISK_REJECTED,
                                          "stock: PM failed")
            return refuse(GateStage.PM_ABSTAIN, "STOCK_PM_FAILED",
                          result.error or "no valid proposal")
        proposal = result.parsed
        await self.journal.record_proposal(proposal, decision_id)
        await self.journal.transition(decision_id, DecisionState.PM_PROPOSED)
        if not proposal.trade:
            await self.journal.transition(decision_id,
                                          DecisionState.RISK_REJECTED,
                                          "stock: PM abstained")
            return refuse(GateStage.PM_ABSTAIN, "PM_ABSTAIN",
                          proposal.abstain_reason or "abstained")
        if proposal.direction is not candidate.direction:
            await self.journal.transition(decision_id,
                                          DecisionState.RISK_REJECTED,
                                          "stock: direction mismatch")
            return refuse(GateStage.PM_ABSTAIN, "STOCK_DIRECTION_MISMATCH",
                          f"PM {proposal.direction} vs scan "
                          f"{candidate.direction}")

        rt_pkg = builder.build("RED_TEAM", cap_tokens=12000,
                               analyst_outputs=assessments,
                               proposal=proposal)
        rt = await self.council.anthropic.call(
            "red_team", self._prompt("rt"), rt_pkg, RedTeamReview,
            decision_id=decision_id, session_id=session_id,
            estimated_cost=0.04)
        if rt.failed or not isinstance(rt.parsed, RedTeamReview):
            await self.journal.transition(decision_id,
                                          DecisionState.RISK_REJECTED,
                                          "stock: red team failed")
            return refuse(GateStage.RED_TEAM, "STOCK_RED_TEAM_FAILED",
                          rt.error or "no valid review")
        review = rt.parsed
        await self.journal.record_review(decision_id,
                                         f"prop_{decision_id[-8:]}_r0",
                                         review)
        await self.journal.transition(decision_id, DecisionState.RED_TEAMED)
        if review.verdict is Verdict.VETO:
            await self.journal.transition(decision_id,
                                          DecisionState.RISK_REJECTED,
                                          "stock: vetoed")
            return refuse(GateStage.RED_TEAM, "RED_TEAM_VETO",
                          review.summary)
        risk_pct = proposal.desired_portfolio_risk_pct
        if review.verdict is Verdict.MODIFY:
            # The brake is applied directly; no revision round for shares.
            risk_pct = min(risk_pct, review.recommended_max_risk_pct)

        # Deterministic expression gates: stop, size, caps.
        snaps = await self.market.snapshots([candidate.symbol])
        snap = snaps.get(candidate.symbol)
        entry_ref = float(snap.signal_price() or snap.mid or 0.0) if snap else 0.0
        ask = float(snap.quote.ask or 0.0) if snap else 0.0
        if entry_ref <= 0:
            await self.journal.transition(decision_id,
                                          DecisionState.RISK_REJECTED,
                                          "stock: no price")
            return refuse(GateStage.DATA_QUALITY, "STOCK_NO_PRICE",
                          "no usable equity price")
        stop = usable_stop(proposal.invalidation, entry_ref,
                           self.min_stop_pct, self.max_stop_pct)
        if stop is None:
            await self.journal.transition(decision_id,
                                          DecisionState.RISK_REJECTED,
                                          "stock: no usable stop")
            return refuse(GateStage.RISK, "STOCK_NO_USABLE_STOP",
                          f"no PRICE stop {self.min_stop_pct}-"
                          f"{self.max_stop_pct}% below {entry_ref:.2f}")
        qty = size_shares(equity, entry_ref, stop, risk_pct,
                          self.risk_cap_pct, self.max_position_pct, headroom)
        if qty < 1:
            await self.journal.transition(decision_id,
                                          DecisionState.RISK_REJECTED,
                                          "stock: sized to zero")
            return refuse(GateStage.RISK, "STOCK_QTY_ZERO",
                          f"risk {risk_pct:.2f}% stop {stop:.2f} "
                          f"headroom {headroom:,.0f}")
        if not execute:
            await self.journal.transition(decision_id,
                                          DecisionState.RISK_REJECTED,
                                          "stock: execution disabled")
            return 0.0

        await self.journal.transition(decision_id,
                                      DecisionState.ORDER_SUBMITTED)
        filled = await submit_marketable(
            self.orders, self.db, sleeve="STOCK", symbol=candidate.symbol,
            side="buy", qty=qty, reference_price=ask or entry_ref * 1.001,
            reason=f"council {decision_id}", decision_id=decision_id)
        if filled is None:
            await self.journal.transition(decision_id, DecisionState.NO_FILL)
            return refuse(GateStage.EXECUTION, "EXEC_NO_FILL",
                          "stock buy unfilled at marketable limit")

        fill_price = float(filled.get("filled_avg_price") or entry_ref)
        fill_qty = int(float(filled.get("filled_qty") or qty))
        target = round(fill_price + self.target_r * (fill_price - stop), 2)
        now = utc_now()
        trade_id = f"slvtr_{new_uuid()[:12]}"
        await self.db.execute(
            "INSERT INTO sleeve_trades(trade_id, decision_id, symbol, qty, "
            "entry_price, stop_price, target_price, horizon_days, opened_at, "
            "status, thesis, invalidation_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, decision_id, candidate.symbol, fill_qty, fill_price,
             stop, target, proposal.expected_horizon_days, iso_utc(now),
             "OPEN", proposal.thesis[:500],
             json.dumps([r.model_dump() for r in proposal.invalidation])))
        self.tracked.append(StockPosition(
            trade_id=trade_id, decision_id=decision_id,
            symbol=candidate.symbol, qty=fill_qty, entry_price=fill_price,
            stop_price=stop, target_price=target,
            horizon_days=proposal.expected_horizon_days, opened_at=now))
        await self.journal.transition(decision_id, DecisionState.FILLED,
                                      f"stock {fill_qty} @ {fill_price:.2f}")
        await self.db.log_event(
            "INFO", "stock_sleeve", "STOCK_OPENED",
            f"{candidate.symbol} {fill_qty} @ {fill_price:.2f}, stop "
            f"{stop:.2f}, target {target:.2f}",
            {"decision_id": decision_id, "risk_pct": risk_pct})
        return fill_qty * fill_price

    # ---- monitoring ----------------------------------------------------

    async def poll(self, now: datetime | None = None,
                   execute: bool = True) -> None:
        if not self.tracked:
            return
        now = now or utc_now()
        snaps = await self.market.snapshots(sorted(self.held_symbols()))
        for position in list(self.tracked):
            snap = snaps.get(position.symbol)
            price = (float(snap.signal_price() or snap.mid or 0.0)
                     if snap else 0.0)
            if price <= 0:
                continue
            reason, detail = evaluate_stock_exit(
                position, price, now,
                stop_confirm_minutes=self.stop_confirm_minutes,
                horizon_multiple=self.horizon_multiple)
            if reason is None:
                continue
            if not is_rth(now):
                if position.trade_id not in self._exit_awaiting_rth:
                    self._exit_awaiting_rth.add(position.trade_id)
                    await self.db.log_event(
                        "INFO", "stock_sleeve", "STOCK_EXIT_AWAITING_OPEN",
                        f"{position.symbol}: {reason} outside RTH; sell "
                        "queues for the open", {"detail": detail})
                continue
            self._exit_awaiting_rth.discard(position.trade_id)
            if execute:
                await self.close(position, reason, detail, price)

    async def close(self, position: StockPosition, reason: str,
                    detail: str, reference_price: float) -> bool:
        bid = reference_price
        snaps = await self.market.snapshots([position.symbol])
        snap = snaps.get(position.symbol)
        if snap and snap.quote.bid:
            bid = float(snap.quote.bid)
        filled = await submit_marketable(
            self.orders, self.db, sleeve="STOCK", symbol=position.symbol,
            side="sell", qty=position.qty, reference_price=bid,
            reason=f"{reason}: {detail}"[:120],
            decision_id=position.decision_id)
        if filled is None:
            await self.db.log_event(
                "WARN", "stock_sleeve", "STOCK_CLOSE_UNFILLED",
                f"{position.symbol} sell unfilled; will retry next poll")
            return False
        exit_price = float(filled.get("filled_avg_price") or bid)
        pnl = round((exit_price - position.entry_price) * position.qty, 2)
        await self.db.execute(
            "UPDATE sleeve_trades SET status='CLOSED', closed_at=?, "
            "exit_reason=?, exit_price=?, realized_pnl=? WHERE trade_id=?",
            (iso_utc(utc_now()), reason, exit_price, pnl, position.trade_id))
        self.tracked = [p for p in self.tracked
                        if p.trade_id != position.trade_id]
        await self.db.log_event(
            "INFO", "stock_sleeve", "STOCK_CLOSED",
            f"{position.symbol} {position.qty} @ {exit_price:.2f} "
            f"({reason}) P&L ${pnl:,.2f}",
            {"decision_id": position.decision_id, "detail": detail})
        return True

    async def snapshot(self, now: datetime | None = None) -> int:
        state = await read_accounting(self.orders, self.db,
                                      self.held_symbols())
        rows = [{"symbol": s, **vals} for s, vals in state.by_symbol.items()]
        return await record_snapshots(self.db, "STOCK", rows,
                                      now or utc_now())

    async def liquidate_all(self, execute: bool = False) -> list[str]:
        actions = []
        for position in list(self.tracked):
            actions.append(f"{'SELL' if execute else 'would sell'} "
                           f"{position.qty} {position.symbol}")
            if execute:
                await self.close(position, "MANUAL", "liquidate_all",
                                 position.entry_price)
        return actions
