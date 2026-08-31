"""
Alpha Council v2.5 - one candidate through the full council, no execution.

Exercises the entire decision pipeline with REAL model calls: quant scan,
options engine, Bull/Bear/Catalyst, Portfolio Manager, structure selection,
Claude Red Team, one revision, and the Risk Constitution. It stops
immediately before order submission.

Cost is roughly $0.13 per run at current prices. That is the cheapest
possible way to find out whether the GPT-5.6 and Sonnet 5 parameter shapes
in config are correct, whether the evidence packs are sized sensibly, and
whether the agents return schema-valid output.

Run this before trusting anything to a scheduler.

Place at: scripts/council_once.py

Usage:
    # deterministic path only, no model calls, free
    uv run python scripts/council_once.py --symbol SPY --no-council

    # full council with real API calls
    uv run python scripts/council_once.py --symbol SPY

    # outside market hours, relax staleness to exercise the pipeline
    uv run python scripts/council_once.py --symbol SPY --allow-stale
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.agents.budget import BudgetManager  # noqa: E402
from alpha_council.agents.council import Council, effective_risk_pct  # noqa: E402
from alpha_council.agents.evidence import EvidenceBuilder  # noqa: E402
from alpha_council.agents.llm import AnthropicClient, OpenAIClient  # noqa: E402
from alpha_council.alpaca.market_data import MarketDataService  # noqa: E402
from alpha_council.alpaca.rest_client import AlpacaRestClient  # noqa: E402
from alpha_council.alpaca.screeners import AssetCatalog, ScreenerService  # noqa: E402
from alpha_council.db.config_store import ensure_config_version  # noqa: E402
from alpha_council.db.engine import Database  # noqa: E402
from alpha_council.evolution.payoffs import PayoffEngine  # noqa: E402
from alpha_council.evolution.scenarios import ScenarioGenerator  # noqa: E402
from alpha_council.evolution.payoffs import PayoffEngine  # noqa: E402
from alpha_council.evolution.scenarios import ScenarioGenerator  # noqa: E402
from alpha_council.intelligence.news import NewsIntelligence  # noqa: E402
from alpha_council.journal.trade_journal import TradeJournal  # noqa: E402
from alpha_council.models.enums import (  # noqa: E402
    CandidateTrack,
    DataConfidence,
    Direction,
    RiskDecision,
    Verdict,
)
from alpha_council.options_engine.chain import ChainFilters, ChainService  # noqa: E402
from alpha_council.options_engine.spreads import SpreadBuilder, SpreadFilters  # noqa: E402
from alpha_council.quant.discovery import DiscoveryService, UniverseManager  # noqa: E402
from alpha_council.quant.scoring import (  # noqa: E402
    IntelSummary,
    build_candidate,
    classify_track,
    summarize_intel,
)
from alpha_council.risk.constitution import (  # noqa: E402
    PortfolioState,
    RiskConstitution,
    TradeRequest,
    load_blackouts,
    sector_of,
)
from alpha_council.settings import get_settings, load_yaml  # noqa: E402
from alpha_council.utils.ids import decision_id as make_decision_id  # noqa: E402
from alpha_council.utils.time import utc_now  # noqa: E402


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say("")
    say("=" * 74)
    say(title)
    say("=" * 74)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.assert_paper_only()

    scoring = load_yaml("scoring")
    risk_cfg = load_yaml("risk_constitution")
    universe_cfg = load_yaml("universe")
    calendar = load_yaml("event_calendar")

    tier_cfg = scoring.get("tiers", {}).get(args.tier, {})
    options_cfg = dict(scoring.get("options", {}))
    config_version = scoring.get("config_version", settings.config_version)

    if args.allow_stale:
        # Test-only. This run cannot execute, so a relaxed staleness bound
        # exercises the pipeline outside market hours without risk.
        options_cfg["max_quote_lag_seconds"] = 864_000
        options_cfg["max_underlying_drift_pct"] = 1.0
        options_cfg["fresh_quote_seconds"] = 864_000

    decision = make_decision_id()
    session = f"sess_{decision[-8:]}"

    rule("ALPHA COUNCIL - SINGLE COUNCIL SESSION")
    say(f"  symbol    : {args.symbol}")
    say(f"  tier      : {args.tier}")
    say(f"  decision  : {decision}")
    say(f"  council   : {'DISABLED (deterministic only)' if args.no_council else 'ENABLED (real API calls)'}")
    if args.allow_stale:
        say("  staleness : RELAXED - test mode, execution impossible")

    async with Database(settings.database_path) as db, \
            AlpacaRestClient(settings, scoring) as api:

        await ensure_config_version(db, config_version, scoring, risk_cfg,
                                    tier=args.tier, note="council_once")

        market = MarketDataService(api, db)
        catalog = AssetCatalog(api)
        screeners = ScreenerService(api, db)
        universe = UniverseManager([args.symbol, args.benchmark],
                                   cap=10, ttl_minutes=90)
        discovery = DiscoveryService(market, catalog, screeners, universe, db,
                                     {**scoring, "discovery": {
                                         "enable_most_active": False,
                                         "enable_movers": False,
                                         "stage0_top_n": 5}})
        chains = ChainService(api, market,
                              options_cfg.get("chain_cache_seconds", 60))

        # ---- 1. quant ------------------------------------------------
        rule("1. QUANT SCAN")
        if args.force_direction:
            # Test affordance only. The ambiguity floor exists to stop the
            # system trading a direction it cannot justify; bypassing it is
            # never acceptable in production, and this script cannot execute.
            discovery.config = {
                **discovery.config,
                "tiers": {**scoring.get("tiers", {}),
                          1: {**tier_cfg, "direction_ambiguity_floor": 0.0}},
            }
        stage0 = await discovery.stage0([args.symbol],
                                        benchmark=args.benchmark, top_n=5)
        if not stage0:
            say("  No Stage-0 result. Common causes:")
            for symbol, gate, detail in universe.rejected:
                say(f"    {symbol}: {gate} - {detail}")
            say("")
            say("  DIR_AMBIGUOUS outside market hours is expected. Try a")
            say("  different symbol or run during a session.")
            return 1

        result = stage0[0]
        direction = (Direction.BULLISH if result.direction > 0
                     else Direction.BEARISH)
        say(f"  direction      : {direction} ({result.combined_direction:+.3f})")
        say(f"  fast score     : {result.fast_score:.1f}")
        say(f"  momentum       : {result.momentum:.1f}")
        say(f"  relative volume: {result.relative_volume:.1f}")
        say(f"  rel strength   : {result.relative_strength:.1f}")
        say(f"  trend/regime   : {result.trend_regime:.1f}")
        say(f"  last price     : {result.indicators.last_price:.2f}")

        news = NewsIntelligence(api, db, scoring)
        snap = (await market.snapshots([args.symbol])).get(args.symbol)
        day_return = None
        price = snap.quote.signal_price() or snap.mid if snap else None
        if snap and snap.prev_close and price:
            day_return = (price - snap.prev_close) / snap.prev_close
        events = await news.collect([args.symbol], lookback_hours=24,
                                    price_returns={args.symbol: day_return}
                                    if day_return is not None else {})
        intel = summarize_intel(events.get(args.symbol, []))
        track = classify_track(intel, result.source, direction)
        say(f"  news events    : {intel.event_count}")
        say(f"  catalyst       : "
            f"{intel.catalyst_score:.1f}" if intel.event_count
            else "  catalyst       : none")
        say(f"  track          : {track}")

        candidate = build_candidate(
            result, intel, track, as_of=utc_now(),
            options_opportunity=0.0, options_liquidity=0.0,
            weights=scoring, tier=args.tier, config_version=config_version)
        say(f"  pre score      : {candidate.pre_score:.1f}")

        # ---- 2. options ----------------------------------------------
        rule("2. OPTIONS ENGINE")
        cfilters = ChainFilters.from_tier(tier_cfg, options_cfg)
        chain = await chains.fetch(args.symbol, result.indicators.last_price,
                                   cfilters)
        say(f"  contracts seen : {chain.contracts_seen}")
        say(f"  usable legs    : {chain.usable}")
        say(f"  worst lag      : {chain.max_quote_lag:.1f}s")
        for gate, count in sorted(chain.rejection_counts().items(),
                                  key=lambda kv: -kv[1])[:6]:
            say(f"    {gate:<30} {count}")

        if chain.usable < 2:
            say("  Not enough usable legs. Stopping.")
            return 1

        sfilters = SpreadFilters.from_config(tier_cfg, options_cfg)
        builder = SpreadBuilder(sfilters, scoring.get("structure_weights"),
                                scoring.get("leg_liquidity_weights"))
        spreads = builder.build(chain, direction)
        say(f"  combinations   : {spreads.combinations_tried}")
        say(f"  structures     : {len(spreads.structures)}")
        for gate, count in sorted(spreads.rejection_counts().items(),
                                  key=lambda kv: -kv[1])[:6]:
            say(f"    {gate:<30} {count}")

        if not spreads.structures:
            say("  No valid structure. Stopping.")
            return 1

        say("")
        say(f"  {'#':<3}{'STRIKES':<16}{'DTE':>5}{'DEBIT':>8}{'C/W':>7}"
            f"{'RR':>7}{'LIQ':>7}{'SCORE':>8}")
        say("  " + "-" * 62)
        for s in spreads.structures:
            say(f"  {s.rank:<3}{s.long_leg.strike:g}/{s.short_leg.strike:g}"
                f"{'':<6}{s.dte:>5}{s.initial_limit_debit:>8.2f}"
                f"{s.cost_to_width_ratio:>7.3f}{s.reward_risk_ratio:>7.2f}"
                f"{s.liquidity_score:>7.1f}{s.structure_score:>8.1f}")

        scored = build_candidate(
            result, IntelSummary(), CandidateTrack.MOMENTUM, as_of=utc_now(),
            options_opportunity=spreads.options_opportunity_score,
            options_liquidity=spreads.options_liquidity_score,
            weights=scoring, tier=args.tier, config_version=config_version)
        floor = float(tier_cfg.get("final_score_floor", 68.0))
        say("")
        say(f"  final score    : {scored.final_opportunity_score:.1f} "
            f"(tier floor {floor:.1f})")
        if scored.final_opportunity_score < floor:
            say("  Below the tier floor. In production this stops here;")
            say("  continuing so the council path can be exercised.")

        if args.no_council:
            rule("DETERMINISTIC PATH COMPLETE")
            say("  Everything up to the council works. No model was called.")
            say(f"  client: {api.stats()}  chains: {chains.stats()}")
            return 0

        # ---- 3. council ----------------------------------------------
        rule("3. COUNCIL")

        # agent_runs.decision_id is a foreign key. Seed the parent chain
        # (scan_runs -> candidate_scores -> decisions) or every journalled
        # agent call fails with IntegrityError before any model is reached.
        scan_id = f"scan_once_{decision[-8:]}"
        candidate_id = f"cand_once_{decision[-8:]}"
        now_iso = utc_now().isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO scan_runs(scan_id, mode, config_version, "
            "started_at, universe_size, candidate_count, status) "
            "VALUES(?,'COUNCIL_ONCE',?,?,1,1,'COMPLETE')",
            (scan_id, config_version, now_iso))
        await db.execute(
            "INSERT OR IGNORE INTO candidate_scores(candidate_id, scan_id, "
            "config_version, symbol, direction, as_of, momentum_score, "
            "relative_volume_score, trend_regime_score, "
            "relative_strength_score, options_opportunity_score, "
            "options_liquidity_score, data_confidence_factor, regime_factor, "
            "event_risk_factor, fast_score, pre_score, "
            "raw_opportunity_score, final_opportunity_score, "
            "discovery_source, candidate_track, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (candidate_id, scan_id, config_version, args.symbol,
             str(direction), now_iso, scored.momentum_score,
             scored.relative_volume_score, scored.trend_regime_score,
             scored.relative_strength_score,
             scored.options_opportunity_score, scored.options_liquidity_score,
             scored.data_confidence_factor, scored.regime_factor,
             scored.event_risk_factor, scored.fast_score, scored.pre_score,
             scored.raw_opportunity_score, scored.final_opportunity_score,
             str(scored.discovery_source), str(scored.track), now_iso))
        await TradeJournal(db).open_decision(
            decision, candidate_id, args.symbol, config_version,
            str(scored.discovery_source), scored.track)

        budget = BudgetManager(db, scoring)
        await budget.load()
        budget.start_session(session)
        say(f"  budget before  : {budget.summary()}")

        if not (settings.has_openai() and settings.has_anthropic()):
            say("  Missing an API key. Set OPENAI_API_KEY and "
                "ANTHROPIC_API_KEY in .env.")
            return 1

        openai_client = OpenAIClient(
            db, budget, scoring, settings.openai_api_key.get_secret_value())
        council = Council(
            openai_client,
            AnthropicClient(db, budget, scoring,
                            settings.anthropic_api_key.get_secret_value()),
            scoring,
            scenarios=ScenarioGenerator(openai_client, PayoffEngine(db),
                                        db, scoring))

        evidence = EvidenceBuilder(
            candidate=scored, intel_events=[],
            structures=spreads.structures,
            portfolio_state={"equity": 100000.0, "open_positions": 0,
                             "open_risk_pct": 0.0},
            market_summary={"last_price": result.indicators.last_price,
                            "rvol": result.metrics.get("rvol"),
                            "session": "test"},
            scheduled_events=[])

        say("  running Bull, Bear, Catalyst, PM, selection, Red Team...")
        outcome = await council.run(scored, spreads.structures, evidence,
                                    decision, session)

        say("")
        say(f"  analysts       : {len(outcome.assessments)}/3")
        for assessment in outcome.assessments:
            say(f"    {assessment.analyst:<9} score {assessment.score:>5.1f}  "
                f"confidence {assessment.confidence:.2f}")
        if outcome.degraded:
            say(f"  degraded       : {outcome.degraded}")

        if outcome.proposal is not None:
            p = outcome.proposal
            say("")
            say(f"  PM decision    : {'TRADE' if p.trade else 'ABSTAIN'}")
            say(f"  direction      : {p.direction}")
            say(f"  confidence     : {p.confidence:.2f}")
            say(f"  requested risk : {p.desired_portfolio_risk_pct:.2f}%")
            say(f"  horizon        : {p.expected_horizon_days} days")
            say(f"  thesis         : {p.thesis[:200]}")
            if p.abstain_reason:
                say(f"  abstain reason : {p.abstain_reason}")
            for rule_item in p.invalidation:
                say(f"  invalidation   : {rule_item.rule_type} "
                    f"{rule_item.comparator} {rule_item.threshold} - "
                    f"{rule_item.description[:60]}")

        if outcome.selected_structure is not None:
            s = outcome.selected_structure
            say(f"  selected       : rank {s.rank}, "
                f"{s.long_leg.strike:g}/{s.short_leg.strike:g}, "
                f"debit {s.initial_limit_debit:.2f}")

        if outcome.review is not None:
            r = outcome.review
            say("")
            say(f"  RED TEAM       : {r.verdict}")
            say(f"  risk score     : {r.risk_score}/10")
            say(f"  fatal flaw     : {r.fatal_flaw}")
            say(f"  max risk rec   : {r.recommended_max_risk_pct:.2f}%")
            say(f"  summary        : {r.summary[:200]}")
            say(f"  counterarg     : {r.strongest_counterargument[:200]}")
            for problem in r.problems:
                say(f"    [{problem.severity:>2}] {problem.category:<12} "
                    f"{problem.description[:80]}")

        if outcome.revision is not None:
            rev = outcome.revision
            say("")
            say(f"  REVISION       : {'TRADE' if rev.trade else 'ABSTAIN'}")
            say(f"  revised risk   : {rev.desired_portfolio_risk_pct:.2f}%")
            say(f"  revised rank   : {rev.selected_structure_rank}")

        say("")
        say(f"  council result : {outcome.summary()}")

        if not outcome.traded:
            rule("COUNCIL STOPPED")
            say(f"  stage  : {outcome.stopped_at}")
            say(f"  gate   : {outcome.gate_id}")
            say(f"  reason : {outcome.reason[:300]}")
            say(f"  cost   : ${outcome.cost_usd:.4f} across {outcome.calls} calls")
            say("")
            say("  A stop here is a normal outcome, not a failure.")
            return 0

        # ---- 4. risk --------------------------------------------------
        rule("4. RISK CONSTITUTION")
        selected = outcome.selected_structure
        portfolio = PortfolioState(equity=100000.0, day_start_equity=100000.0,
                                   peak_equity=100000.0)
        constitution = RiskConstitution(risk_cfg, scoring,
                                        load_blackouts(calendar))
        request = TradeRequest(
            decision_id=decision, symbol=args.symbol,
            sector=sector_of(args.symbol, universe_cfg.get("sectors", {})),
            direction=direction, structure=selected,
            desired_risk_pct=effective_risk_pct(outcome),
            pm_confidence=outcome.final_proposal.confidence,
            red_team_verdict=outcome.review.verdict if outcome.review
            else Verdict.PASS,
            red_team_max_risk_pct=(outcome.review.recommended_max_risk_pct
                                   if outcome.review else None),
            equity_data_confidence=DataConfidence.HIGH,
            option_data_confidence=DataConfidence.HIGH,
            final_opportunity_score=scored.final_opportunity_score,
            market_open=True)

        evaluation = constitution.evaluate(request, portfolio, tier=args.tier,
                                           config_version=config_version)
        say(f"  decision       : {evaluation.decision}")
        say(f"  requested qty  : {evaluation.requested_qty}")
        say(f"  approved qty   : {evaluation.approved_qty}")
        say(f"  approved risk  : ${evaluation.approved_max_loss:,.2f}")
        for violation in evaluation.violations:
            say(f"    [{violation.severity}] {violation.rule_id}: "
                f"{violation.message}")

        rule("SUMMARY")
        say(f"  would trade    : "
            f"{evaluation.decision not in (RiskDecision.REJECT, RiskDecision.HALT)}")
        say(f"  council cost   : ${outcome.cost_usd:.4f} "
            f"across {outcome.calls} calls")
        say(f"  budget after   : {budget.summary()}")
        say(f"  client         : {api.stats()}")
        say("")
        say("  NO ORDER WAS SUBMITTED. This script cannot execute.")

        usage = await budget.by_purpose()
        if usage:
            say("")
            say(f"  {'PURPOSE':<22}{'MODEL':<18}{'CALLS':>6}{'IN':>8}"
                f"{'OUT':>8}{'COST':>9}")
            say("  " + "-" * 68)
            for row in usage:
                say(f"  {row['purpose']:<22}{row['model']:<18}"
                    f"{row['calls']:>6}{row['in_tok']:>8}"
                    f"{row['out_tok']:>8}${row['cost']:>8.4f}")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--tier", type=int, default=1, choices=[1, 2, 3])
    ap.add_argument("--no-council", action="store_true",
                    help="deterministic path only; no model calls, no cost")
    ap.add_argument("--allow-stale", action="store_true",
                    help="relax quote staleness to exercise the pipeline "
                         "outside market hours (test only)")
    ap.add_argument("--force-direction", choices=["BULLISH", "BEARISH"],
                    help="override the ambiguity floor for pipeline testing")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
