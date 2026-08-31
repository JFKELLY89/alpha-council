"""
Alpha Council v2.5 - the scheduler.

Drives the trading day. Every job is isolated: an exception inside one
never stops the scheduler, because a failed 11:30 scan must not cost you
the 13:30 scan and the position monitor that runs between them.

Three properties matter more than the timetable:

  ISOLATION. Each job body is wrapped. Failures land in system_events and
  the loop continues.

  NO OVERLAP. max_instances=1 per job. Two concurrent scans would evaluate
  the same candidate twice and could double-submit.

  BREADTH BEFORE LOOSENESS. The 11:00 and 14:00 jobs widen the search; the
  12:30 and 14:15 jobs relax quality. That ordering is the anti-zero-trade
  mechanism and it is enforced by the schedule itself.

Place at: alpha_council/scheduler.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Awaitable, Callable, Sequence

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from alpha_council.agents.evidence import EvidenceBuilder
from alpha_council.db.engine import Database
from alpha_council.intelligence.news import NewsIntelligence
from alpha_council.journal.trade_journal import RejectionLog
from alpha_council.quant.scoring import IntelSummary, summarize_intel
from alpha_council.models.enums import DataConfidence, ExitReason
from alpha_council.orchestrator import (
    DecisionOutcome,
    Orchestrator,
    SessionSummary,
    TierManager,
)
from alpha_council.quant.scanner import FunnelScanner
from alpha_council.risk.constitution import PortfolioState
from alpha_council.utils.ids import scan_id as make_scan_id
from alpha_council.utils.time import (
    ET,
    COMPETITION_LAST_SESSION,
    et_now,
    is_trading_day,
    parse_et_time,
    to_et,
    utc_now,
)

MISFIRE_GRACE = 300          # a job late by more than 5 minutes is skipped


@dataclass(slots=True)
class SchedulerConfig:
    scan_times: list[str] = field(default_factory=lambda: [
        "09:40", "10:15", "11:30", "13:30", "15:00"])
    discovery_refresh: str = "09:35"
    breadth_checks: list[str] = field(default_factory=lambda: ["11:00", "14:00"])
    tier_checks: list[str] = field(default_factory=lambda: ["12:30", "14:15"])
    monitor_seconds: int = 120
    event_loop_seconds: int = 300
    new_trade_cutoff: str = "15:35"
    lessons_time: str = "16:15"
    flatten_time: str = "15:45"

    @classmethod
    def from_config(cls, scoring: dict[str, Any]) -> "SchedulerConfig":
        s = scoring.get("schedule", {})
        return cls(
            scan_times=list(s.get("full_scans_et", cls().scan_times)),
            discovery_refresh=s.get("discovery_refresh_et", "09:35"),
            breadth_checks=list(s.get("breadth_check_et", ["11:00", "14:00"])),
            tier_checks=list(s.get("tier_check_et", ["12:30", "14:15"])),
            monitor_seconds=int(s.get("position_monitor_seconds", 120)),
            event_loop_seconds=int(s.get("event_loop_seconds", 300)),
            lessons_time=s.get("lessons_et", "16:15"),
        )


class TradingSession:
    """Holds every service and exposes the scheduled jobs."""

    def __init__(self, *, db: Database, scanner: FunnelScanner,
                 orchestrator: Orchestrator, tiers: TierManager,
                 monitor: Any, shadows: Any, journal: Any, orders: Any,
                 market: Any, screeners: Any, budget: Any,
                 config: dict[str, Any], risk_config: dict[str, Any],
                 universe_config: dict[str, Any], control: Any = None,
                 news: NewsIntelligence | None = None,
                 max_trades: int | None = None, dry_run: bool = False):
        self.db = db
        self.scanner = scanner
        self.orchestrator = orchestrator
        self.tiers = tiers
        self.monitor = monitor
        self.shadows = shadows
        self.journal = journal
        self.orders = orders
        self.market = market
        self.screeners = screeners
        self.budget = budget
        self.control = control
        self.news = news
        self.config = config
        self.risk_config = risk_config
        self.universe_config = universe_config
        self.max_trades = max_trades
        self.dry_run = dry_run

        self.summary = SessionSummary(
            session_id=f"sess_{to_et(utc_now()):%Y%m%d}",
            started_at=utc_now())
        self.cutoff_reached = False
        self.halted = False
        self._scan_lock = asyncio.Lock()

    # ---- guards ------------------------------------------------------

    @property
    def trades_remaining(self) -> bool:
        if self.max_trades is None:
            return True
        return self.summary.trades_opened < self.max_trades

    def can_open(self) -> tuple[bool, str]:
        if self.halted:
            return False, "session halted"
        if self.cutoff_reached:
            return False, "past the new-trade cutoff"
        if not self.trades_remaining:
            return False, f"trade ceiling {self.max_trades} reached"
        if self.dry_run:
            return False, "dry run"
        return True, ""

    async def log(self, level: str, event: str, message: str,
                  context: dict[str, Any] | None = None) -> None:
        await self.db.log_event(level, "scheduler", event, message,
                                context or {})

    # ---- jobs ---------------------------------------------------------

    async def refresh_discovery(self) -> None:
        result = await self.screeners.probe_entitlements()
        await self.log("INFO", "DISCOVERY_REFRESH",
                       "screener entitlements probed", result)

    async def full_scan(self) -> None:
        """One complete funnel pass, then councils on the survivors."""
        if self._scan_lock.locked():
            await self.log("WARN", "SCAN_SKIPPED",
                           "previous scan still running")
            return

        async with self._scan_lock:
            scan_id = make_scan_id()
            self.summary.scans += 1
            rejections = RejectionLog(self.db, self.tiers.config_version,
                                      self.tiers.tier)

            portfolio = await self._portfolio_state()
            if portfolio is None:
                await self.log("ERROR", "SCAN_ABORTED",
                               "could not read account state")
                return

            # Discovery runs here so news can be fetched for the dynamic
            # universe, not just Core. scanner.run() refreshes again, which
            # costs two screener requests and keeps the scanner's contract
            # unchanged.
            now = utc_now()
            symbols = await self.scanner.discovery.refresh(now=now)
            intel, benchmark_return, _ = await self._gather_intelligence(
                symbols, now)

            result = await self.scanner.run(
                scan_id, tier=self.tiers.tier, intel_by_symbol=intel,
                benchmark_return=benchmark_return, now=now)
            await self.scanner.persist(result)

            await self.log("INFO", "SCAN_COMPLETE",
                           f"{len(result.final)} final candidates",
                           {"scan_id": scan_id,
                            "discovery": len(result.discovery_symbols),
                            "stage0": len(result.stage0),
                            "prescored": len(result.prescored),
                            "final": len(result.final),
                            "event_track": sum(
                                1 for c in result.final
                                if str(c.track) == "EVENT"),
                            "benchmark_return": round(benchmark_return, 5),
                            "tier": self.tiers.tier})

            allowed, reason = self.can_open()
            max_councils = int(
                self.tiers.tier_config().get("max_councils_per_scan", 3))

            for candidate in result.final[:max_councils]:
                structures = result.structures_for(candidate.symbol)
                if not structures:
                    continue

                self.summary.candidates_evaluated += 1
                builder = EvidenceBuilder(
                    candidate=candidate, intel_events=[],
                    structures=structures,
                    portfolio_state=self._portfolio_summary(portfolio),
                    market_summary={"tier": self.tiers.tier},
                    scheduled_events=[])

                outcome = await self.orchestrator.evaluate_candidate(
                    candidate, f"cand_{scan_id[-8:]}_{candidate.symbol}",
                    structures, builder, portfolio, self.summary.session_id,
                    equity_confidence=DataConfidence.HIGH,
                    option_confidence=DataConfidence.HIGH,
                    rejections=rejections, execute=allowed)

                self.summary.councils_run += 1
                self.summary.outcomes.append(outcome)
                self.summary.cost_usd += outcome.cost_usd
                if outcome.traded:
                    self.summary.trades_opened += 1
                    allowed, reason = self.can_open()

            written = await rejections.flush()
            await self.log("INFO", "SCAN_REJECTIONS",
                           f"{written} gate rejections recorded",
                           {"scan_id": scan_id})

    async def breadth_check(self) -> None:
        """Widen the search. Never touches quality thresholds."""
        transitions = [t for t in self.tiers.evaluate(utc_now())
                       if t.startswith("BREADTH")]
        if transitions:
            await self.tiers.persist_changes(self.db, transitions,
                                             self.risk_config)
            self.summary.tier_changes.extend(transitions)
            await self.log("INFO", "BREADTH_EXPANDED", ", ".join(transitions),
                           {"breadth_level": self.tiers.breadth_level})

    async def tier_check(self) -> None:
        """Relax quality gates, only after breadth has already expanded."""
        transitions = self.tiers.evaluate(utc_now())
        if transitions:
            await self.tiers.persist_changes(self.db, transitions,
                                             self.risk_config)
            self.summary.tier_changes.extend(transitions)
            await self.log("INFO", "TIER_CHANGED", ", ".join(transitions),
                           {"tier": self.tiers.tier,
                            "config_version": self.tiers.config_version})

    async def monitor_positions(self) -> None:
        decisions = await self.monitor.poll(execute=not self.dry_run)
        for decision_id, exit_decision in decisions:
            if exit_decision.should_exit:
                await self.log("INFO", "EXIT_DECISION",
                               f"{decision_id}: {exit_decision.reason}",
                               {"detail": exit_decision.detail,
                                "advisory": exit_decision.advisory})

    async def mark_shadows(self) -> None:
        """Mark every open shadow variant on one schedule.

        Same cycle, same method, every variant, or the attribution
        arithmetic compares things that were valued differently.
        """
        rows = await self.db.fetchall(
            "SELECT DISTINCT decision_id FROM shadow_trades "
            "WHERE status = 'OPEN'")
        now = utc_now()
        for row in rows:
            decision_id = row["decision_id"]
            await self.shadows.mark_all(decision_id, now)
            result = self.shadows.compute(decision_id, now)
            if result is not None:
                await self.shadows.persist(result)

    async def enforce_cutoff(self) -> None:
        self.cutoff_reached = True
        await self.log("INFO", "NEW_TRADE_CUTOFF",
                       "no further entries this session",
                       {"trades_opened": self.summary.trades_opened})

    async def competition_flatten(self) -> None:
        """Realized P&L in the submission beats open marks."""
        if to_et(utc_now()).date() != COMPETITION_LAST_SESSION:
            return
        outcomes = await self.monitor.flatten_all(
            ExitReason.COMPETITION_FLATTEN)
        await self.log("WARN", "COMPETITION_FLATTEN",
                       f"closed {len(outcomes)} position(s)")

    async def post_close(self) -> None:
        performance = await self.journal.performance()
        report = self.summary.report()
        await self.log("INFO", "SESSION_REPORT",
                       f"{report['trades_opened']} trades, "
                       f"${report['cost_usd']:.4f} spend",
                       {"report": report, "performance": performance,
                        "budget": self.budget.summary()})

    # ---- helpers ------------------------------------------------------

    async def _gather_intelligence(
        self, symbols: Sequence[str], now: datetime
    ) -> tuple[dict[str, IntelSummary], float, dict[str, float]]:
        """Fetch news and price response for the discovery universe.

        Returns (intel by symbol, benchmark return, day returns).

        Every failure path returns empty rather than raising. A news outage
        degrades every candidate to the MOMENTUM track, which is a worse
        scan but still a scan; letting it abort would cost the whole cycle.
        """
        empty: dict[str, IntelSummary] = {}
        try:
            snapshots = await self.market.snapshots(list(symbols))
        except Exception as exc:  # noqa: BLE001
            await self.log("WARN", "INTEL_SNAPSHOTS_FAILED", str(exc)[:200])
            return empty, 0.0, {}

        returns: dict[str, float] = {}
        for symbol, snap in snapshots.items():
            prev_close = snap.prev_close
            price = snap.quote.signal_price() or snap.mid
            if prev_close and price and prev_close > 0:
                returns[symbol] = (price - prev_close) / prev_close

        benchmark = returns.get(
            self.universe_config.get("benchmarks", {}).get("broad", "SPY"), 0.0)

        if self.news is None:
            return empty, benchmark, returns

        try:
            events = await self.news.collect(
                list(symbols), lookback_hours=int(
                    self.config.get("discovery", {}).get(
                        "news_lookback_hours", 8)),
                price_returns=returns, now=now)
        except Exception as exc:  # noqa: BLE001
            await self.log("WARN", "INTEL_COLLECT_FAILED", str(exc)[:200])
            return empty, benchmark, returns

        intel = {symbol: summarize_intel(evs)
                 for symbol, evs in events.items() if evs}
        material = sum(1 for s in intel.values() if s.has_material_catalyst)
        await self.log(
            "INFO", "INTEL_COLLECTED",
            f"{self.news.stats.events} events, {material} material",
            {**self.news.stats.as_dict(), "benchmark_return": benchmark})
        return intel, benchmark, returns

    async def _portfolio_state(self) -> PortfolioState | None:
        # MCP first when available, REST otherwise. ControlPlane records
        # which transport served the call, so the MCP share is measured.
        try:
            if self.control is not None:
                account = await self.control.get_account()
            else:
                account = await self.orders.api.get_account()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(account, dict):
            return None

        equity = float(account.get("equity", 0) or 0)
        if equity <= 0:
            return None
        day_start = float(account.get("last_equity", equity) or equity)

        peak = await self.db.get_state("peak_equity", default=equity)
        peak = max(float(peak or equity), equity)
        await self.db.set_state("peak_equity", peak)
        await self.db.set_state("day_start_equity", day_start)

        open_rows = await self.db.fetchall(
            "SELECT decision_id, qty, entry_debit FROM trade_journal "
            "WHERE status = 'OPEN'")
        open_risk = sum(float(r["entry_debit"] or 0) * 100
                        * int(r["qty"] or 0) for r in open_rows)

        return PortfolioState(
            equity=equity, day_start_equity=day_start, peak_equity=peak,
            open_risk_dollars=open_risk,
            open_position_count=len(open_rows),
            open_decision_ids={r["decision_id"] for r in open_rows})

    @staticmethod
    def _portfolio_summary(state: PortfolioState) -> dict[str, Any]:
        return {
            "equity": round(state.equity, 2),
            "open_positions": state.open_position_count,
            "open_risk_pct": round(
                state.open_risk_dollars / state.equity * 100, 3),
            "daily_drawdown_pct": round(state.daily_drawdown_pct, 3),
        }


# ======================================================================

def build_scheduler(session: TradingSession,
                    config: SchedulerConfig) -> AsyncIOScheduler:
    """Wire the timetable. Every job is isolated and non-overlapping."""
    scheduler = AsyncIOScheduler(timezone=ET)

    def add(func: Callable[[], Awaitable[None]], trigger: Any,
            job_id: str) -> None:
        scheduler.add_job(
            _guarded(session, func, job_id), trigger, id=job_id,
            max_instances=1, misfire_grace_time=MISFIRE_GRACE,
            coalesce=True, replace_existing=True)

    def cron(hhmm: str) -> CronTrigger:
        moment = parse_et_time(hhmm)
        return CronTrigger(day_of_week="mon-fri", hour=moment.hour,
                           minute=moment.minute, timezone=ET)

    add(session.refresh_discovery, cron(config.discovery_refresh),
        "discovery_refresh")

    for index, hhmm in enumerate(config.scan_times):
        add(session.full_scan, cron(hhmm), f"scan_{index}")

    for index, hhmm in enumerate(config.breadth_checks):
        add(session.breadth_check, cron(hhmm), f"breadth_{index}")

    for index, hhmm in enumerate(config.tier_checks):
        add(session.tier_check, cron(hhmm), f"tier_{index}")

    add(session.monitor_positions,
        IntervalTrigger(seconds=config.monitor_seconds, timezone=ET),
        "position_monitor")

    add(session.mark_shadows,
        IntervalTrigger(seconds=config.event_loop_seconds, timezone=ET),
        "shadow_marks")

    add(session.enforce_cutoff, cron(config.new_trade_cutoff), "cutoff")
    add(session.competition_flatten, cron(config.flatten_time), "flatten")
    add(session.post_close, cron(config.lessons_time), "post_close")

    return scheduler


def _guarded(session: TradingSession, func: Callable[[], Awaitable[None]],
             job_id: str) -> Callable[[], Awaitable[None]]:
    """Wrap a job so a failure is recorded and the scheduler survives.

    An exception in the 11:30 scan must not cost the 13:30 scan or the
    position monitor running between them.
    """

    async def wrapper() -> None:
        # Intraday jobs do nothing on a holiday or a weekend.
        if not is_trading_day(et_now().date()):
            return
        try:
            await func()
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            try:
                await session.log(
                    "ERROR", "JOB_FAILED",
                    f"{job_id}: {type(exc).__name__}: {exc}"[:400],
                    {"job_id": job_id})
            except Exception:  # noqa: BLE001
                pass
            print(f"[scheduler] {job_id} failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)

    return wrapper
