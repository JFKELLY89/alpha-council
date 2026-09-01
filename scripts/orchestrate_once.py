"""
Alpha Council v2.5 - orchestrator trace.

Runs ONE candidate through the exact orchestrator path the scheduler uses,
printing a timestamp at every stage. The scheduler swallows this detail:
its jobs log only on completion or failure, so a slow stage and a hung one
look identical from outside.

This answers the question the logs cannot: where does evaluate_candidate
actually spend its time, and does it finish?

Defaults to --dry so it can be run any time without submitting.

Place at: scripts/orchestrate_once.py

Usage:
    uv run python scripts/orchestrate_once.py
    uv run python scripts/orchestrate_once.py --symbol CRWD --tier 3 --allow-stale
    uv run python scripts/orchestrate_once.py --live-paper
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.agents.budget import BudgetManager  # noqa: E402
from alpha_council.agents.council import Council  # noqa: E402
from alpha_council.agents.evidence import EvidenceBuilder  # noqa: E402
from alpha_council.agents.llm import AnthropicClient, OpenAIClient  # noqa: E402
from alpha_council.alpaca.market_data import MarketDataService  # noqa: E402
from alpha_council.alpaca.rest_client import AlpacaRestClient  # noqa: E402
from alpha_council.alpaca.screeners import AssetCatalog, ScreenerService  # noqa: E402
from alpha_council.db.config_store import ensure_config_version  # noqa: E402
from alpha_council.db.engine import Database  # noqa: E402
from alpha_council.evolution.payoffs import PayoffEngine  # noqa: E402
from alpha_council.evolution.scenarios import ScenarioGenerator  # noqa: E402
from alpha_council.execution.order_manager import OrderManager  # noqa: E402
from alpha_council.execution.position_monitor import PositionMonitor  # noqa: E402
from alpha_council.intelligence.news import NewsIntelligence  # noqa: E402
from alpha_council.journal.marks import LiveMarkSource  # noqa: E402
from alpha_council.journal.shadow_book import ShadowBook  # noqa: E402
from alpha_council.journal.trade_journal import RejectionLog, TradeJournal  # noqa: E402
from alpha_council.models.enums import DataConfidence, MarkMethod  # noqa: E402
from alpha_council.options_engine.chain import ChainService  # noqa: E402
from alpha_council.orchestrator import Orchestrator, TierManager  # noqa: E402
from alpha_council.quant.discovery import DiscoveryService, UniverseManager  # noqa: E402
from alpha_council.quant.scanner import FunnelScanner  # noqa: E402
from alpha_council.quant.scoring import summarize_intel  # noqa: E402
from alpha_council.risk.constitution import (  # noqa: E402
    PortfolioState,
    RiskConstitution,
    load_blackouts,
)
from alpha_council.settings import get_settings, load_yaml  # noqa: E402
from alpha_council.utils.ids import scan_id as make_scan_id  # noqa: E402
from alpha_council.utils.time import utc_now  # noqa: E402

START = time.monotonic()


def say(msg: str = "") -> None:
    print(f"[{time.monotonic() - START:>7.2f}s] {msg}", flush=True)


def rule(title: str) -> None:
    print("", flush=True)
    print("=" * 74, flush=True)
    print(f"{title}", flush=True)
    print("=" * 74, flush=True)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.assert_paper_only()

    scoring = load_yaml("scoring")
    risk_cfg = load_yaml("risk_constitution")
    universe_cfg = load_yaml("universe")
    calendar = load_yaml("event_calendar")
    config_version = scoring.get("config_version", settings.config_version)

    options_cfg = dict(scoring.get("options", {}))
    if args.allow_stale:
        options_cfg["max_quote_lag_seconds"] = 864_000
        options_cfg["fresh_quote_seconds"] = 864_000
        options_cfg["max_underlying_drift_pct"] = 1.0
        scoring = {**scoring, "options": options_cfg}

    rule("ORCHESTRATOR TRACE")
    say(f"symbol   : {args.symbol or 'top candidate from scan'}")
    say(f"tier     : {args.tier}")
    say(f"mode     : {'LIVE PAPER' if args.live_paper else 'DRY RUN'}")

    async with Database(settings.database_path) as db, \
            AlpacaRestClient(settings, scoring) as api:

        await ensure_config_version(db, config_version, scoring, risk_cfg,
                                    tier=args.tier, note="orchestrator trace")

        rule("1. BUILD SERVICES")
        market = MarketDataService(api, db)
        catalog = AssetCatalog(api)
        await catalog.load()
        say(f"catalog loaded, {catalog.options_enabled_count:,} optionable")

        screeners = ScreenerService(api, db)
        excluded = {e["symbol"] for e in universe_cfg.get("exclusions", [])}
        core = [s for s in universe_cfg.get("core_symbols", [])
                if s not in excluded]
        if args.symbol:
            core = [args.symbol]

        disc_cfg = scoring.get("discovery", {})
        universe = UniverseManager(
            core, cap=int(disc_cfg.get("max_dynamic_symbols", 250)),
            ttl_minutes=int(disc_cfg.get("dynamic_ttl_minutes", 90)),
            exclusions=excluded,
            name_lookup=lambda s: (catalog.get(s).name if catalog.get(s) else ""))
        discovery = DiscoveryService(market, catalog, screeners, universe,
                                     db, scoring)
        chains = ChainService(api, market,
                              int(options_cfg.get("chain_cache_seconds", 60)))
        scanner = FunnelScanner(discovery, chains, db, scoring)

        budget = BudgetManager(db, scoring)
        await budget.load()
        openai_client = OpenAIClient(
            db, budget, scoring, settings.openai_api_key.get_secret_value())
        council = Council(
            openai_client,
            AnthropicClient(db, budget, scoring,
                            settings.anthropic_api_key.get_secret_value()),
            scoring,
            scenarios=ScenarioGenerator(openai_client, PayoffEngine(db),
                                        db, scoring))

        constitution = RiskConstitution(risk_cfg, scoring,
                                        load_blackouts(calendar))
        orders = OrderManager(api, db)
        journal = TradeJournal(db)
        marks = LiveMarkSource(api, market)
        shadows = ShadowBook(db, marks, MarkMethod.ADJUSTED_MID)
        monitor = PositionMonitor(db, market, orders, journal, scoring,
                                  risk_cfg)
        tiers = TierManager(scoring, config_version)
        tiers.start_session()
        tiers.state.tier = args.tier
        orchestrator = Orchestrator(db, council, constitution, orders,
                                    journal, shadows, monitor, tiers,
                                    scoring, universe_cfg)
        news = NewsIntelligence(api, db, scoring)
        say("services built")

        rule("2. SCAN")
        scan_id = make_scan_id()
        now = utc_now()
        symbols = await discovery.refresh(now=now)
        say(f"discovery: {len(symbols)} symbols")

        snapshots = await market.snapshots(symbols)
        returns = {}
        for symbol, snap in snapshots.items():
            price = snap.quote.signal_price() or snap.mid
            if snap.prev_close and price and snap.prev_close > 0:
                returns[symbol] = (price - snap.prev_close) / snap.prev_close
        benchmark = returns.get("SPY", 0.0)
        say(f"price returns: {len(returns)} symbols, SPY {benchmark:+.2%}")

        events = await news.collect(
            symbols, lookback_hours=int(disc_cfg.get("news_lookback_hours", 8)),
            price_returns=returns, now=now)
        intel = {s: summarize_intel(e) for s, e in events.items() if e}
        say(f"intelligence: {news.stats.events} events, {len(intel)} symbols")

        result = await scanner.run(scan_id, tier=args.tier,
                                   intel_by_symbol=intel,
                                   benchmark_return=benchmark, now=now)
        await scanner.persist(result)
        snapshot = result.snapshot()
        say(f"funnel: {snapshot.discovery_count} -> "
            f"{snapshot.stage0_survivors} -> {snapshot.prescore_survivors} "
            f"-> {snapshot.options_prescreened} -> {len(result.final)}")

        if not result.final:
            rule("NO CANDIDATES")
            say("The scan produced nothing to orchestrate.")
            for symbol, gate, detail, stage in result.rejections[:8]:
                say(f"  {symbol:<7}{str(stage):<22}{gate:<28}{str(detail)[:40]}")
            return 0

        candidate = result.final[0]
        structures = result.structures_for(candidate.symbol)
        say(f"candidate: {candidate.symbol} {candidate.direction} "
            f"score {candidate.final_opportunity_score:.1f} "
            f"track {candidate.track}, {len(structures)} structures")

        rule("3. ORCHESTRATE")
        say("calling evaluate_candidate; every stage below is timed")

        portfolio = await _portfolio(api, db)
        if portfolio is None:
            say("could not read account state")
            return 1
        say(f"portfolio: equity ${portfolio.equity:,.2f}, "
            f"{portfolio.open_position_count} open")

        builder = EvidenceBuilder(
            candidate=candidate,
            intel_events=events.get(candidate.symbol, []),
            structures=structures,
            portfolio_state={"equity": portfolio.equity,
                             "open_positions": portfolio.open_position_count},
            market_summary={"benchmark_return": round(benchmark, 5)},
            scheduled_events=[])
        rejections = RejectionLog(db, config_version, args.tier)

        outcome = await orchestrator.evaluate_candidate(
            candidate, result.candidate_ids[candidate.symbol],
            structures, builder, portfolio, "trace",
            equity_confidence=DataConfidence.HIGH,
            option_confidence=DataConfidence.HIGH,
            rejections=rejections, execute=args.live_paper)

        await rejections.flush()

        rule("4. OUTCOME")
        for key, value in outcome.summary().items():
            say(f"{key:<18}: {value}")
        say(f"reason            : {outcome.reason[:200]}")

        state = await journal.state_of(outcome.decision_id)
        say(f"final state       : {state}")

        rule("5. WHAT PERSISTED")
        for table, where in [
                ("decisions", f"decision_id='{outcome.decision_id}'"),
                ("trade_proposals", f"decision_id='{outcome.decision_id}'"),
                ("option_structures", f"decision_id='{outcome.decision_id}'"),
                ("red_team_reviews", f"decision_id='{outcome.decision_id}'"),
                ("risk_evaluations", f"decision_id='{outcome.decision_id}'"),
                ("shadow_trades", f"decision_id='{outcome.decision_id}'"),
                ("orders", f"decision_id='{outcome.decision_id}'")]:
            n = await db.fetchvalue(f"SELECT COUNT(*) FROM {table} WHERE {where}")
            say(f"{table:<20}: {n}")

        say("")
        say(f"budget: {budget.summary()['openai']}")

    return 0


async def _portfolio(api, db) -> PortfolioState | None:
    try:
        account = await api.get_account()
    except Exception:  # noqa: BLE001
        return None
    equity = float(account.get("equity", 0) or 0)
    if equity <= 0:
        return None
    rows = await db.fetchall(
        "SELECT decision_id, qty, entry_debit FROM trade_journal "
        "WHERE status='OPEN'")
    open_risk = sum(float(r["entry_debit"] or 0) * 100 * int(r["qty"] or 0)
                    for r in rows)
    return PortfolioState(
        equity=equity,
        day_start_equity=float(account.get("last_equity", equity) or equity),
        peak_equity=equity, open_risk_dollars=open_risk,
        open_position_count=len(rows),
        open_decision_ids={r["decision_id"] for r in rows})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="")
    ap.add_argument("--tier", type=int, default=1, choices=[1, 2, 3])
    ap.add_argument("--allow-stale", action="store_true")
    ap.add_argument("--live-paper", action="store_true",
                    help="actually submit if the risk gate approves")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
