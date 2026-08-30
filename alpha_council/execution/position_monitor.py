"""
Alpha Council v2.5 - position monitoring and exits.

The governing constraint: exits are driven by the UNDERLYING, which is
real-time on IEX, never by option marks, which come from a delayed and
derived Indicative feed.

That is not a stylistic preference. If option data goes stale or blocked
mid-session, a system whose stops depend on option prices has no way to
exit. Every primary trigger here is computable from the underlying alone,
so positions remain manageable when the options feed degrades and when both
LLM providers are down.

Option-based triggers exist but are ADVISORY: they only fire when data
quality is HIGH or MEDIUM, and they can never be the sole reason a
position stays open.

Place at: alpha_council/execution/position_monitor.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Sequence

from alpha_council.alpaca.market_data import MarketDataService
from alpha_council.db.engine import Database
from alpha_council.execution.order_manager import ExecutionOutcome, OrderManager
from alpha_council.journal.trade_journal import TradeJournal
from alpha_council.models.enums import (
    DataConfidence,
    Direction,
    ExitReason,
    StrategyType,
)
from alpha_council.models.trading import InvalidationRule, OptionStructure
from alpha_council.utils.time import (
    COMPETITION_FLATTEN_ET,
    iso_utc,
    to_et,
    utc_now,
)

ADVISORY_CONFIDENCES = {DataConfidence.HIGH, DataConfidence.MEDIUM}


@dataclass(slots=True)
class MonitoredPosition:
    decision_id: str
    symbol: str
    structure: OptionStructure
    qty: int
    entry_debit: float
    opened_at: datetime
    invalidation: list[InvalidationRule] = field(default_factory=list)
    horizon_days: int = 5

    @property
    def direction(self) -> Direction:
        return self.structure.strategy.direction

    @property
    def short_strike(self) -> float:
        return self.structure.short_leg.strike

    @property
    def max_profit(self) -> float:
        return self.structure.max_profit_per_spread

    def dte(self, now: datetime) -> int:
        return (self.structure.expiration - to_et(now).date()).days

    def unrealized(self, mark: float) -> float:
        return round((mark - self.entry_debit) * 100.0 * self.qty, 2)


@dataclass(slots=True)
class ExitDecision:
    should_exit: bool
    reason: ExitReason | None = None
    detail: str = ""
    advisory: bool = False
    triggers_evaluated: list[str] = field(default_factory=list)

    @property
    def gate_id(self) -> str:
        return f"EXIT_{self.reason}" if self.reason else "EXIT_NONE"


def evaluate_exit(position: MonitoredPosition, underlying: float,
                  now: datetime, config: dict[str, Any],
                  spread_mark: float | None = None,
                  option_confidence: DataConfidence = DataConfidence.BLOCKED,
                  vwap: float | None = None) -> ExitDecision:
    """Pure exit evaluation. No I/O, no LLM, fully testable.

    Primary triggers use the underlying only. Secondary triggers use the
    option mark and are skipped entirely unless data quality permits.
    """
    exits = config.get("exits", {})
    primary = exits.get("primary", {})
    secondary = exits.get("secondary", {})
    evaluated: list[str] = []

    # --- competition flatten: unconditional -------------------------
    if to_et(now) >= COMPETITION_FLATTEN_ET:
        return ExitDecision(True, ExitReason.COMPETITION_FLATTEN,
                            "competition flatten time reached",
                            triggers_evaluated=["COMPETITION_FLATTEN"])
    evaluated.append("COMPETITION_FLATTEN")

    # --- time stop --------------------------------------------------
    time_stop_dte = int(primary.get("time_stop_dte", 2))
    dte = position.dte(now)
    evaluated.append("TIME_STOP")
    if dte <= time_stop_dte:
        return ExitDecision(True, ExitReason.TIME_STOP,
                            f"{dte} DTE at or below the {time_stop_dte} floor",
                            triggers_evaluated=evaluated)

    # --- underlying target -------------------------------------------
    evaluated.append("UNDERLYING_TARGET")
    if primary.get("underlying_target_at_short_strike", True):
        if position.direction is Direction.BULLISH:
            hit = underlying >= position.short_strike
        else:
            hit = underlying <= position.short_strike
        if hit:
            return ExitDecision(
                True, ExitReason.UNDERLYING_TARGET,
                f"underlying {underlying:.2f} reached the short strike "
                f"{position.short_strike:.2f}",
                triggers_evaluated=evaluated)

    # --- PM invalidation ---------------------------------------------
    evaluated.append("UNDERLYING_INVALIDATION")
    if primary.get("honor_pm_invalidation_rules", True):
        fired = check_invalidation(position.invalidation, underlying, vwap,
                                   position.opened_at, now)
        if fired is not None:
            return ExitDecision(True, ExitReason.UNDERLYING_INVALIDATION,
                                fired, triggers_evaluated=evaluated)

    # --- advisory option-based triggers -------------------------------
    required = {DataConfidence(c) for c in
                secondary.get("require_data_confidence", ["HIGH", "MEDIUM"])}
    if spread_mark is None or option_confidence not in (required
                                                        or ADVISORY_CONFIDENCES):
        # Option data is unusable. The position stays managed by the
        # underlying triggers above; it is never stranded.
        return ExitDecision(False, detail="option data unusable for advisory "
                                          "triggers", triggers_evaluated=evaluated)

    evaluated.append("PROFIT_TARGET")
    target_pct = float(secondary.get("profit_target_pct_of_max", 0.55))
    profit_per_spread = (spread_mark - position.entry_debit) * 100.0
    if position.max_profit > 0 and profit_per_spread >= target_pct * position.max_profit:
        return ExitDecision(
            True, ExitReason.PROFIT_TARGET,
            f"${profit_per_spread:.2f}/spread is {target_pct:.0%} of the "
            f"${position.max_profit:.2f} maximum",
            advisory=True, triggers_evaluated=evaluated)

    evaluated.append("PREMIUM_STOP")
    stop_pct = float(secondary.get("premium_stop_pct_of_entry", 0.45))
    if spread_mark <= stop_pct * position.entry_debit:
        return ExitDecision(
            True, ExitReason.PREMIUM_STOP,
            f"mark {spread_mark:.2f} at or below {stop_pct:.0%} of the "
            f"{position.entry_debit:.2f} entry debit",
            advisory=True, triggers_evaluated=evaluated)

    return ExitDecision(False, triggers_evaluated=evaluated)


def check_invalidation(rules: Sequence[InvalidationRule], underlying: float,
                       vwap: float | None, opened_at: datetime,
                       now: datetime) -> str | None:
    """Evaluate PM invalidation rules against the underlying.

    Rules referencing anything the system cannot observe in real time are
    skipped rather than guessed at. The PM prompt forbids option-price
    rules for exactly this reason.
    """
    for rule in rules:
        if rule.threshold is None or rule.comparator is None:
            continue

        if rule.rule_type == "PRICE":
            observed = underlying
        elif rule.rule_type == "VWAP":
            if vwap is None:
                continue
            observed = vwap
        elif rule.rule_type == "TIME":
            elapsed_days = (now - opened_at).total_seconds() / 86400.0
            observed = elapsed_days
        else:
            # CATALYST and COMPOSITE rules need evidence the monitor does
            # not carry. They are handled by the council, not here.
            continue

        if _comparator_fires(observed, rule.comparator, rule.threshold):
            return (f"{rule.rule_type} invalidation: {observed:.2f} "
                    f"{rule.comparator} {rule.threshold:.2f} "
                    f"({rule.description[:60]})")
    return None


def _comparator_fires(observed: float, comparator: str,
                      threshold: float) -> bool:
    return {
        "LT": observed < threshold,
        "LTE": observed <= threshold,
        "GT": observed > threshold,
        "GTE": observed >= threshold,
    }.get(comparator, False)


class PositionMonitor:
    """Polls open positions and exits them on deterministic rules."""

    def __init__(self, db: Database, market: MarketDataService,
                 orders: OrderManager, journal: TradeJournal,
                 config: dict[str, Any], risk_config: dict[str, Any]):
        self.db = db
        self.market = market
        self.orders = orders
        self.journal = journal
        self.config = config
        self.risk_config = risk_config
        self._positions: dict[str, MonitoredPosition] = {}

    def track(self, position: MonitoredPosition) -> None:
        self._positions[position.decision_id] = position

    def untrack(self, decision_id: str) -> None:
        self._positions.pop(decision_id, None)

    @property
    def tracked(self) -> list[MonitoredPosition]:
        return list(self._positions.values())

    async def restore(self) -> int:
        """Rebuild tracking from the database after a restart.

        A monitor that forgets its positions on restart is worse than no
        monitor: the position is live and nothing is watching it.
        """
        rows = await self.db.fetchall(
            "SELECT t.decision_id, t.qty, t.entry_debit, t.opened_at, "
            "t.invalidation_json, s.raw_json AS structure_json "
            "FROM trade_journal t "
            "LEFT JOIN option_structures s ON s.decision_id = t.decision_id "
            "WHERE t.status='OPEN'")

        restored = 0
        for row in rows:
            if not row.get("structure_json"):
                continue
            try:
                structure = OptionStructure.model_validate_json(
                    row["structure_json"])
                rules = [InvalidationRule.model_validate(r)
                         for r in json.loads(row["invalidation_json"] or "[]")]
            except Exception:  # noqa: BLE001 - one bad row must not stop restore
                await self.db.log_event(
                    "ERROR", "position_monitor", "RESTORE_FAILED",
                    f"could not rebuild {row['decision_id']}",
                    decision_id=row["decision_id"])
                continue

            from alpha_council.utils.time import parse_alpaca_ts

            self.track(MonitoredPosition(
                decision_id=row["decision_id"], symbol=structure.symbol,
                structure=structure, qty=int(row["qty"] or 0),
                entry_debit=float(row["entry_debit"] or 0.0),
                opened_at=parse_alpaca_ts(row["opened_at"]) or utc_now(),
                invalidation=rules))
            restored += 1
        return restored

    # ---- the poll ---------------------------------------------------

    async def poll(self, now: datetime | None = None,
                   execute: bool = True) -> list[tuple[str, ExitDecision]]:
        """One monitoring cycle. Returns every exit decision made."""
        now = now or utc_now()
        decisions: list[tuple[str, ExitDecision]] = []
        if not self._positions:
            return decisions

        symbols = sorted({p.symbol for p in self._positions.values()})
        snapshots = await self.market.snapshots(symbols)

        for position in list(self._positions.values()):
            snap = snapshots.get(position.symbol)
            underlying = snap.signal_price(
                self.config.get("equity", {}).get(
                    "prefer_last_trade_above_spread_pct", 0.010)
            ) if snap else None

            if underlying is None:
                # No usable underlying quote. Nothing can be evaluated
                # safely, so nothing is done. Logged, never guessed.
                await self.db.log_event(
                    "WARN", "position_monitor", "NO_UNDERLYING_QUOTE",
                    f"cannot evaluate exits for {position.symbol}",
                    decision_id=position.decision_id)
                continue

            confidence = self._equity_confidence(snap.quote_age)
            decision = evaluate_exit(
                position, underlying, now, self.risk_config,
                spread_mark=None, option_confidence=DataConfidence.BLOCKED,
                vwap=None)

            decisions.append((position.decision_id, decision))
            await self._record_snapshot(position, underlying, decision, now)

            if decision.should_exit and execute:
                await self.close(position, decision, now)
        return decisions

    def _equity_confidence(self, age: float | None) -> DataConfidence:
        cfg = self.config.get("equity", {})
        if age is None or age > 120:
            return DataConfidence.BLOCKED
        if age <= float(cfg.get("pre_submit_max_lag_seconds", 5)):
            return DataConfidence.HIGH
        if age <= float(cfg.get("scan_max_lag_seconds", 30)):
            return DataConfidence.MEDIUM
        return DataConfidence.DEGRADED

    async def _record_snapshot(self, position: MonitoredPosition,
                               underlying: float, decision: ExitDecision,
                               now: datetime) -> None:
        from alpha_council.utils.ids import new_uuid

        await self.db.execute(
            "INSERT INTO position_snapshots(snapshot_id, captured_at, symbol, "
            "qty, market_value, cost_basis, unrealized_pl, unrealized_plpc, "
            "raw_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (new_uuid(), iso_utc(now), position.symbol, position.qty, None,
             position.entry_debit * 100 * position.qty, None, None,
             json.dumps({"underlying": underlying,
                         "dte": position.dte(now),
                         "exit_reason": str(decision.reason)
                         if decision.reason else None,
                         "triggers": decision.triggers_evaluated})))

    async def close(self, position: MonitoredPosition,
                    decision: ExitDecision,
                    now: datetime | None = None) -> ExecutionOutcome:
        """Submit a closing order and journal the result."""
        now = now or utc_now()
        await self.db.log_event(
            "INFO", "position_monitor", "EXIT_TRIGGERED",
            f"{position.symbol}: {decision.reason} - {decision.detail}",
            {"reason": str(decision.reason), "advisory": decision.advisory},
            decision_id=position.decision_id)

        outcome = await self.orders.execute_with_walk(
            position.structure, f"{position.decision_id}_x", position.qty,
            max_allowed_debit=position.structure.natural_debit, closing=True)

        if outcome.filled and outcome.fill_debit is not None:
            await self.journal.close_trade(
                position.decision_id, exit_credit=outcome.fill_debit,
                reason=decision.reason or ExitReason.MANUAL, closed_at=now)
            self.untrack(position.decision_id)
        else:
            # An unfilled exit is a live risk, not a closed book. The
            # position stays tracked and will be retried next poll.
            await self.db.log_event(
                "ERROR", "position_monitor", "EXIT_NOT_FILLED",
                f"{position.symbol} exit did not fill; still open",
                {"final_status": outcome.final_status},
                decision_id=position.decision_id)
        return outcome

    async def flatten_all(self, reason: ExitReason = ExitReason.COMPETITION_FLATTEN,
                          now: datetime | None = None) -> list[ExecutionOutcome]:
        """Close everything. Used at the competition flatten and on HALT."""
        now = now or utc_now()
        outcomes = []
        for position in list(self._positions.values()):
            decision = ExitDecision(True, reason, "flatten requested")
            outcomes.append(await self.close(position, decision, now))
        return outcomes
