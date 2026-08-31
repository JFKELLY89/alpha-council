"""
Alpha Council v2.5 - autonomous entry point.

Constructs every service in dependency order, restores state from the
database, and starts the scheduler.

Startup does four things that matter after a restart:

  PAPER-ONLY ASSERTION runs before anything else touches the network.
  POSITION MONITOR RESTORE rebuilds tracking from the database. A monitor
    that forgets its positions on restart is worse than no monitor: the
    position is live and nothing is watching it.
  BUDGET RELOAD recovers committed spend, so a restart cannot reset the
    $50 cap to zero.
  SCREENER PROBE establishes which optional discovery sources are usable
    this session. A 403 disables that source and nothing else.

Place at: scripts/run_alpha_council.py

Usage:
    # every stage runs, nothing is ever submitted
    uv run python scripts/run_alpha_council.py --dry-run

    # first supervised session, hard ceiling of one trade
    uv run python scripts/run_alpha_council.py --max-trades 1

    # run one scan immediately and exit, no scheduler
    uv run python scripts/run_alpha_council.py --scan-now --dry-run

    # fully autonomous
    uv run python scripts/run_alpha_council.py
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.agents.budget import BudgetManager  # noqa: E402
from alpha_council.agents.council import Council  # noqa: E402
from alpha_council.agents.llm import AnthropicClient, OpenAIClient  # noqa: E402
from alpha_council.alpaca.market_data import MarketDataService  # noqa: E402
from alpha_council.alpaca.rest_client import AlpacaRestClient  # noqa: E402
from alpha_council.alpaca.screeners import AssetCatalog, ScreenerService  # noqa: E402
from alpha_council.db.config_store import ensure_config_version  # noqa: E402
from alpha_council.db.engine import Database  # noqa: E402
from alpha_council.execution.order_manager import OrderManager  # noqa: E402
from alpha_council.execution.position_monitor import PositionMonitor  # noqa: E402
from alpha_council.journal.marks import LiveMarkSource  # noqa: E402
from alpha_council.journal.shadow_book import ShadowBook  # noqa: E402
from alpha_council.journal.trade_journal import TradeJournal  # noqa: E402
from alpha_council.models.enums import MarkMethod  # noqa: E402
from alpha_council.options_engine.chain import ChainService  # noqa: E402
from alpha_council.orchestrator import Orchestrator, TierManager  # noqa: E402
from alpha_council.quant.discovery import DiscoveryService, UniverseManager  # noqa: E402
from alpha_council.quant.scanner import FunnelScanner  # noqa: E402
from alpha_council.risk.constitution import RiskConstitution, load_blackouts  # noqa: E402
from alpha_council.scheduler import (  # noqa: E402
    SchedulerConfig,
    TradingSession,
    build_scheduler,
)
from alpha_council.settings import get_settings, load_yaml  # noqa: E402
from alpha_council.utils.time import (  # noqa: E402
    et_now,
    is_trading_day,
    sessions_remaining,
    utc_now,
)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say("")
    say("=" * 74)
    say(title)
    say("=" * 74)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.assert_paper_only()          # before anything touches the network

    scoring = load_yaml("scoring")
    risk_cfg = load_yaml("risk_constitution")
    universe_cfg = load_yaml("universe")
    calendar = load_yaml("event_calendar")
    config_version = scoring.get("config_version", settings.config_version)

    rule("ALPHA COUNCIL - AUTONOMOUS SESSION")
    say(f"  started      : {et_now():%Y-%m-%d %H:%M:%S} ET")
    say(f"  trading day  : {is_trading_day(et_now().date())}")
    say(f"  sessions left: {sessions_remaining()}")
    say(f"  config       : {config_version}")
    say(f"  mode         : {'DRY RUN — nothing will be submitted' if args.dry_run else 'LIVE PAPER TRADING'}")
    if args.max_trades is not None:
        say(f"  trade ceiling: {args.max_trades}")

    if not (settings.has_openai() and settings.has_anthropic()):
        say("  Missing an API key. The council cannot run.")
        return 1

    async with Database(settings.database_path) as db, \
            AlpacaRestClient(settings, scoring) as api:

        await ensure_config_version(db, config_version, scoring, risk_cfg,
                                    tier=1, note="autonomous session")

        # ---- data plane ---------------------------------------------
        rule("1. SERVICES")
        market = MarketDataService(api, db)
        catalog = AssetCatalog(api)
        screeners = ScreenerService(api, db)

        loaded = await catalog.load()
        say(f"  asset catalog    : {loaded:,} assets, "
            f"{catalog.options_enabled_count:,} optionable")
        if catalog.options_detection_failed:
            say("  NOTE: options eligibility field not recognized; "
                "deferring to the contracts endpoint")

        core = [s for s in universe_cfg.get("core_symbols", [])
                if s not in {e["symbol"] for e in
                             universe_cfg.get("exclusions", [])}]
        disc_cfg = scoring.get("discovery", {})
        universe = UniverseManager(
            core, cap=int(disc_cfg.get("max_dynamic_symbols", 250)),
            ttl_minutes=int(disc_cfg.get("dynamic_ttl_minutes", 90)),
            exclusions={e["symbol"] for e in universe_cfg.get("exclusions", [])},
            name_lookup=lambda s: (catalog.get(s).name if catalog.get(s) else ""))
        discovery = DiscoveryService(market, catalog, screeners, universe,
                                     db, scoring)
        options_cfg = scoring.get("options", {})
        chains = ChainService(api, market,
                              int(options_cfg.get("chain_cache_seconds", 60)))
        scanner = FunnelScanner(discovery, chains, db, scoring)
        say(f"  core universe    : {len(core)} symbols")

        # ---- agents --------------------------------------------------
        budget = BudgetManager(db, scoring)
        await budget.load()
        council = Council(
            OpenAIClient(db, budget, scoring,
                         settings.openai_api_key.get_secret_value()),
            AnthropicClient(db, budget, scoring,
                            settings.anthropic_api_key.get_secret_value()),
            scoring)
        say(f"  budget           : {budget.summary()}")

        # ---- risk, execution, journal ---------------------------------
        constitution = RiskConstitution(risk_cfg, scoring,
                                        load_blackouts(calendar))
        blackouts = len(load_blackouts(calendar))
        say(f"  blackout windows : {blackouts}")
        if blackouts == 0:
            say("  WARNING: no blackout windows configured. Verify earnings "
                "dates in config/event_calendar.yaml.")

        orders = OrderManager(api, db)
        journal = TradeJournal(db)
        marks = LiveMarkSource(
            api, market,
            fresh_quote_seconds=float(options_cfg.get("fresh_quote_seconds", 60)),
            max_quote_lag_seconds=float(
                options_cfg.get("max_quote_lag_seconds", 1200)),
            max_underlying_drift_pct=float(
                options_cfg.get("max_underlying_drift_pct", 0.010)))
        shadows = ShadowBook(db, marks, MarkMethod.ADJUSTED_MID)
        monitor = PositionMonitor(db, market, orders, journal, scoring,
                                  risk_cfg)

        tiers = TierManager(scoring, config_version)
        tiers.start_session()
        orchestrator = Orchestrator(db, council, constitution, orders,
                                    journal, shadows, monitor, tiers,
                                    scoring, universe_cfg)

        # ---- restore --------------------------------------------------
        rule("2. RESTORE")
        restored = await monitor.restore()
        say(f"  positions tracked: {restored}")
        if restored:
            for position in monitor.tracked:
                say(f"    {position.symbol} x{position.qty} @ "
                    f"{position.entry_debit:.2f}, {position.dte(utc_now())} DTE")

        performance = await journal.performance()
        say(f"  closed trades    : {performance['closed_trades']}")
        say(f"  realized P&L     : ${performance['total_pnl']:,.2f}")

        # ---- session --------------------------------------------------
        session = TradingSession(
            db=db, scanner=scanner, orchestrator=orchestrator, tiers=tiers,
            monitor=monitor, shadows=shadows, journal=journal, orders=orders,
            market=market, screeners=screeners, budget=budget,
            config=scoring, risk_config=risk_cfg,
            universe_config=universe_cfg,
            max_trades=args.max_trades, dry_run=args.dry_run)

        await db.log_event(
            "INFO", "run_alpha_council", "SESSION_STARTED",
            f"dry_run={args.dry_run} max_trades={args.max_trades}",
            {"config_version": config_version, "tier": tiers.tier,
             "restored_positions": restored})

        # ---- one-shot mode --------------------------------------------
        if args.scan_now:
            rule("3. SINGLE SCAN")
            await session.refresh_discovery()
            await session.full_scan()
            rule("RESULT")
            for line, value in session.summary.report().items():
                say(f"  {line:<22}: {value}")
            return 0

        # ---- scheduled mode -------------------------------------------
        rule("3. SCHEDULER")
        sched_cfg = SchedulerConfig.from_config(scoring)
        scheduler = build_scheduler(session, sched_cfg)
        scheduler.start()

        for job in sorted(scheduler.get_jobs(), key=lambda j: j.id):
            say(f"  {job.id:<22} next {job.next_run_time}")

        say("")
        say("  Running. Ctrl-C to stop.")
        say("  Dashboard: uv run streamlit run dashboard/app.py")

        stop = asyncio.Event()

        def request_stop(*_: object) -> None:
            say("")
            say("  Shutdown requested...")
            stop.set()

        try:
            asyncio.get_running_loop().add_signal_handler(
                signal.SIGINT, request_stop)
        except NotImplementedError:
            # Windows: fall back to KeyboardInterrupt below.
            pass

        try:
            await stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            rule("SHUTDOWN")
            scheduler.shutdown(wait=False)

            # Never leave a working order behind on shutdown.
            try:
                working = await orders.working_orders()
                if working:
                    say(f"  cancelling {len(working)} working order(s)")
                    for order in working:
                        await orders.cancel(order.get("id", ""))
            except Exception as exc:  # noqa: BLE001
                say(f"  could not cancel working orders: {exc}")

            report = session.summary.report()
            for line, value in report.items():
                say(f"  {line:<22}: {value}")
            say(f"  budget               : {budget.summary()}")

            await db.log_event("INFO", "run_alpha_council", "SESSION_ENDED",
                               "clean shutdown", {"report": report})
            if monitor.tracked:
                say("")
                say(f"  {len(monitor.tracked)} position(s) remain OPEN. "
                    "Restart to resume monitoring, or close manually.")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="run every stage but never submit an order")
    ap.add_argument("--max-trades", type=int, default=None,
                    help="hard ceiling on entries this session")
    ap.add_argument("--scan-now", action="store_true",
                    help="run one scan immediately and exit")
    try:
        return asyncio.run(run(ap.parse_args()))
    except KeyboardInterrupt:
        say("")
        say("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
