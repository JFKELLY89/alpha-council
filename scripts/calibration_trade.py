"""
Alpha Council v2.4 §1.5 - supervised calibration lifecycle, fully journaled.

Opens one small defined-risk vertical on a liquid symbol, holds briefly,
and closes it - THROUGH the same machinery the autonomous path uses, so
every layer the demo depends on gets real rows:

    trade_journal (open + close)         execution_calibrations (both sides)
    shadow_trades EXECUTED variant       shadow marks + close-time freeze
    decisions state machine              orders / fills audit trail

This exists because the Aug 31 calibration fills went through an ad-hoc
path and left the books empty: three lifecycles at the broker, zero in
the journal. An engineering test that skips the journal proves nothing
about the system being demonstrated.

The Risk Constitution still evaluates the trade: every HARD gate binds
(paper lock, drawdowns, blackouts, cutoff, structure, DTE, sizing caps,
liquidity floor). Only the PM-confidence and opportunity-score QUALITY
floors are waived, because a lifecycle test has no PM and no score by
construction (TradeRequest.is_calibration_trade).

Usage:
    # dry run: build and price the spread, evaluate risk, submit nothing
    uv run python scripts/calibration_trade.py --symbol SPY

    # the real thing: open, hold 90s, close, reconcile
    uv run python scripts/calibration_trade.py --symbol SPY --execute

    # open only; the position stays journaled and monitor-restorable
    uv run python scripts/calibration_trade.py --execute --skip-close
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.alpaca.market_data import MarketDataService  # noqa: E402
from alpha_council.alpaca.rest_client import AlpacaRestClient  # noqa: E402
from alpha_council.db.config_store import ensure_config_version  # noqa: E402
from alpha_council.db.engine import Database  # noqa: E402
from alpha_council.execution.order_manager import OrderManager  # noqa: E402
from alpha_council.execution.position_monitor import (  # noqa: E402
    ExitDecision,
    MonitoredPosition,
    PositionMonitor,
)
from alpha_council.execution.presubmit import PreSubmitRefresher  # noqa: E402
from alpha_council.journal.marks import LiveMarkSource  # noqa: E402
from alpha_council.journal.shadow_book import ShadowBook  # noqa: E402
from alpha_council.journal.trade_journal import TradeJournal  # noqa: E402
from alpha_council.models.enums import (  # noqa: E402
    CandidateTrack,
    DataConfidence,
    DecisionState,
    Direction,
    ExitReason,
    MarkMethod,
    ShadowVariant,
    Verdict,
)
from alpha_council.options_engine.chain import ChainFilters, ChainService  # noqa: E402
from alpha_council.options_engine.spreads import SpreadBuilder, SpreadFilters  # noqa: E402
from alpha_council.risk.constitution import (  # noqa: E402
    PortfolioState,
    RiskConstitution,
    TradeRequest,
    load_blackouts,
    sector_of,
)
from alpha_council.settings import get_settings, load_yaml  # noqa: E402
from alpha_council.utils.ids import decision_id as make_decision_id  # noqa: E402
from alpha_council.utils.time import iso_utc, utc_now  # noqa: E402


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say("")
    say("=" * 72)
    say(title)
    say("=" * 72)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.assert_paper_only()

    scoring = load_yaml("scoring")
    risk_cfg = load_yaml("risk_constitution")
    calendar = load_yaml("event_calendar")
    tier_cfg = scoring.get("tiers", {}).get(1, {})
    options_cfg = scoring.get("options", {})
    activity = scoring.get("activity_target", {})
    config_version = scoring.get("config_version", settings.config_version)

    allowed_symbols = [s.upper() for s in
                       activity.get("calibration_trade_symbols", ["SPY", "QQQ"])]
    symbol = args.symbol.upper()
    if symbol not in allowed_symbols:
        say(f"{symbol} is not a configured calibration symbol "
            f"({allowed_symbols}). Calibration trades stay on the most "
            "liquid names by design.")
        return 1
    max_qty = int(activity.get("calibration_trade_max_qty", 1))
    decision = make_decision_id()

    rule("ALPHA COUNCIL - CALIBRATION LIFECYCLE")
    say(f"  symbol    : {symbol}")
    say(f"  direction : {args.direction}")
    say(f"  qty cap   : {max_qty}")
    say(f"  decision  : {decision}")
    say(f"  mode      : {'LIVE PAPER LIFECYCLE' if args.execute else 'DRY RUN - nothing submitted'}")

    async with Database(settings.database_path) as db, \
            AlpacaRestClient(settings, scoring) as api:

        await ensure_config_version(db, config_version, scoring, risk_cfg,
                                    tier=1, note="calibration lifecycle")

        market = MarketDataService(api, db)
        chains = ChainService(api, market,
                              int(options_cfg.get("chain_cache_seconds", 60)))
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
                                  risk_cfg, marks=marks, shadows=shadows)
        constitution = RiskConstitution(risk_cfg, scoring,
                                        load_blackouts(calendar))
        presubmit = PreSubmitRefresher(api, market, scoring)

        # ---- 1. account and clock ---------------------------------
        rule("1. ACCOUNT AND CLOCK")
        account = await api.get_account()
        clock = await api.get_clock()
        equity = float(account.get("equity", 0) or 0)
        market_open = bool(clock.get("is_open"))
        say(f"  equity : ${equity:,.2f}")
        say(f"  open   : {market_open}")
        if equity <= 0:
            say("  Unreadable equity. Aborting.")
            return 1
        if not market_open and args.execute:
            say("  Market closed; a lifecycle test needs live fills. Aborting.")
            return 1

        # ---- 2. build a real spread -------------------------------
        rule("2. SPREAD")
        snaps = await market.snapshots([symbol])
        snap = snaps.get(symbol)
        spot = snap.signal_price() if snap else None
        if spot is None:
            say("  No usable underlying quote. Aborting.")
            return 1
        say(f"  spot: {spot:.2f}")

        direction = (Direction.BULLISH if args.direction.upper() == "BULLISH"
                     else Direction.BEARISH)
        chain = await chains.fetch(symbol, spot,
                                   ChainFilters.from_tier(tier_cfg, options_cfg))
        say(f"  chain: {chain.contracts_seen} seen, {chain.usable} usable legs")
        if chain.usable < 2:
            for gate, n in sorted(chain.rejection_counts().items(),
                                  key=lambda x: -x[1])[:5]:
                say(f"    {gate:<28} {n}")
            say("  Chain unusable. Aborting.")
            return 1

        builder = SpreadBuilder(
            SpreadFilters.from_config(tier_cfg, options_cfg),
            scoring.get("structure_weights"),
            scoring.get("leg_liquidity_weights"))
        spreads = builder.build(chain, direction)
        if not spreads.structures:
            say("  No valid structure from current quotes. Aborting.")
            return 1
        structure = spreads.structures[0]
        say(f"  selected  : {structure.long_leg.strike:g}/"
            f"{structure.short_leg.strike:g} {structure.strategy} "
            f"exp {structure.expiration} ({structure.dte} DTE)")
        say(f"  limit     : {structure.initial_limit_debit:.2f} "
            f"(natural {structure.natural_debit:.2f}, "
            f"max loss ${structure.max_loss_per_spread:.0f}/spread)")

        # ---- 3. risk constitution ---------------------------------
        rule("3. RISK CONSTITUTION (hard gates all bind)")
        open_rows = await db.fetchall(
            "SELECT t.decision_id, t.qty, t.entry_debit, d.symbol "
            "FROM trade_journal t LEFT JOIN decisions d "
            "ON d.decision_id = t.decision_id WHERE t.status='OPEN'")
        open_risk = sum(float(r["entry_debit"] or 0) * 100 * int(r["qty"] or 0)
                        for r in open_rows)
        sector_map = risk_cfg.get("sectors", {})
        sector_risk: dict[str, float] = {}
        for r in open_rows:
            sec = sector_of(str(r.get("symbol") or ""), sector_map)
            sector_risk[sec] = sector_risk.get(sec, 0.0) + \
                float(r["entry_debit"] or 0) * 100 * int(r["qty"] or 0)
        peak = float(await db.get_state("peak_equity", equity) or equity)
        portfolio = PortfolioState(
            equity=equity,
            day_start_equity=float(account.get("last_equity", equity) or equity),
            peak_equity=max(peak, equity),
            open_risk_dollars=open_risk,
            sector_risk_dollars=sector_risk,
            open_position_count=len(open_rows),
            open_decision_ids={r["decision_id"] for r in open_rows})

        # Ask for exactly enough risk to buy one spread; the qty cap from
        # activity_target bounds it regardless.
        desired_pct = structure.max_loss_per_spread * 1.5 / equity * 100.0
        request = TradeRequest(
            decision_id=decision, symbol=symbol,
            sector=sector_of(symbol, sector_map),
            direction=direction, structure=structure,
            desired_risk_pct=desired_pct,
            pm_confidence=0.0,                      # no PM: lifecycle test
            red_team_verdict=Verdict.PASS,          # no council convened
            red_team_max_risk_pct=None,
            equity_data_confidence=DataConfidence.HIGH,
            option_data_confidence=DataConfidence.HIGH,
            final_opportunity_score=0.0,            # no score: lifecycle test
            market_open=market_open,
            is_calibration_trade=True)
        evaluation = constitution.evaluate(request, portfolio, tier=1,
                                           config_version=config_version)
        qty = min(evaluation.approved_qty, max_qty)
        say(f"  decision : {evaluation.decision}  qty {evaluation.approved_qty}"
            f" -> capped {qty}")
        for violation in evaluation.violations:
            say(f"    [{violation.severity}] {violation.rule_id}: "
                f"{violation.message}")
        if evaluation.decision.blocks_trade or qty < 1:
            say("  Risk Constitution blocked the lifecycle test. "
                "Hard gates are not waived for calibration; aborting.")
            return 1

        if not args.execute:
            rule("DRY RUN COMPLETE")
            say("  Everything above is real; nothing was submitted. "
                "Re-run with --execute during RTH.")
            return 0

        # ---- 4. journaled open ------------------------------------
        rule("4. OPEN")
        scan_id = f"scan_cal_{decision[-8:]}"
        candidate_id = f"cand_cal_{decision[-8:]}"
        now_iso = iso_utc()
        await db.execute(
            "INSERT OR IGNORE INTO scan_runs(scan_id, mode, config_version, "
            "started_at, universe_size, candidate_count, status) "
            "VALUES(?,'CALIBRATION',?,?,1,1,'COMPLETE')",
            (scan_id, config_version, now_iso))
        await db.execute(
            "INSERT OR IGNORE INTO candidate_scores(candidate_id, scan_id, "
            "config_version, symbol, direction, as_of, momentum_score, "
            "relative_volume_score, trend_regime_score, "
            "relative_strength_score, data_confidence_factor, regime_factor, "
            "event_risk_factor, pre_score, discovery_source, candidate_track, "
            "created_at) VALUES(?,?,?,?,?,?,50,50,50,50,1,1,1,50,'CORE',"
            "'CALIBRATION',?)",
            (candidate_id, scan_id, config_version, symbol,
             str(direction), now_iso, now_iso))
        await journal.open_decision(decision, candidate_id, symbol,
                                    config_version, "CORE",
                                    CandidateTrack.CALIBRATION)
        await journal.record_structures(decision, [structure], candidate_id)
        await journal.record_risk(evaluation, f"prop_{decision[-8:]}_r0",
                                  structure.structure_id)

        # §17.4: reprice from live quotes immediately before submission.
        refresh = await presubmit.refresh(structure, tier_cfg)
        underlying_at_submit = spot
        if refresh.ok and refresh.structure is not None:
            structure = refresh.structure
            underlying_at_submit = refresh.underlying_price or spot
            say(f"  pre-submit refresh: limit {structure.initial_limit_debit:.2f}"
                f" underlying {underlying_at_submit:.2f}")
        else:
            say(f"  pre-submit refresh failed ({refresh.gate_id}): "
                f"{refresh.reason}. Aborting before submission.")
            await journal.transition(decision, DecisionState.REJECTED,
                                     (refresh.reason or "")[:120])
            return 1

        await journal.transition(decision, DecisionState.ORDER_SUBMITTED)
        budget = evaluation.approved_risk_budget or evaluation.approved_max_loss
        max_debit = max(budget / max(1, qty) / 100.0,
                        structure.initial_limit_debit)
        outcome = await orders.execute_with_walk(structure, decision, qty,
                                                 max_debit)
        await orders.record_calibration(
            outcome, structure, CandidateTrack.CALIBRATION, direction,
            underlying_at_submit=underlying_at_submit)

        if not outcome.filled or outcome.fill_debit is None:
            say(f"  NOT FILLED after {outcome.walk_steps} attempt(s): "
                f"{outcome.final_status}. Nothing is open; nothing to close.")
            await journal.transition(decision, DecisionState.NO_FILL)
            return 1

        say(f"  FILLED {qty} @ {outcome.fill_debit:.2f} "
            f"({outcome.walk_steps} walk step(s), "
            f"{outcome.seconds_to_fill or 0:.1f}s)")
        await journal.transition(decision, DecisionState.FILLED)
        await journal.open_trade(
            decision, symbol, qty, outcome.fill_debit,
            thesis="calibration lifecycle test (§1.5): execution, "
                   "journaling, and fill-calibration exercise",
            invalidation=[], track=CandidateTrack.CALIBRATION,
            opened_at=outcome.filled_at or utc_now())
        await shadows.create(decision, ShadowVariant.EXECUTED, structure,
                             qty, entry_debit=outcome.fill_debit,
                             entry_timestamp=outcome.filled_at or utc_now())
        position = MonitoredPosition(
            decision_id=decision, symbol=symbol, structure=structure,
            qty=qty, entry_debit=outcome.fill_debit,
            opened_at=outcome.filled_at or utc_now(),
            track=CandidateTrack.CALIBRATION)
        monitor.track(position)

        if args.skip_close:
            rule("OPEN POSITION LEFT UNDER MANAGEMENT")
            say("  The trade is journaled; run_alpha_council will restore "
                "and monitor it, or close with scripts/close_all.py.")
            return 0

        # ---- 5. hold, then journaled close ------------------------
        rule(f"5. HOLD {args.hold_seconds}s, THEN CLOSE")
        await asyncio.sleep(max(0, args.hold_seconds))
        close_outcome = await monitor.close(
            position,
            ExitDecision(True, ExitReason.MANUAL,
                         "calibration lifecycle close"))

        if not close_outcome.filled or close_outcome.fill_debit is None:
            say(f"  CLOSE NOT FILLED: {close_outcome.final_status}. "
                "THE POSITION IS STILL OPEN and journaled. Close it with "
                "scripts/close_all.py --execute, or restart "
                "run_alpha_council to resume monitoring.")
            return 1

        realized = round((close_outcome.fill_debit - outcome.fill_debit)
                         * 100 * qty, 2)
        say(f"  CLOSED @ credit {close_outcome.fill_debit:.2f}  "
            f"realized {realized:+.2f}")

        # ---- 6. reconciliation ------------------------------------
        rule("6. RECONCILIATION")
        for label, sql in (
            ("trade_journal rows", "SELECT COUNT(*) c FROM trade_journal "
             "WHERE decision_id=?"),
            ("shadow trades", "SELECT COUNT(*) c FROM shadow_trades "
             "WHERE decision_id=?"),
            ("shadow marks", "SELECT COUNT(*) c FROM shadow_marks WHERE "
             "shadow_id IN (SELECT shadow_id FROM shadow_trades WHERE "
             "decision_id=?)"),
            ("execution calibrations", "SELECT COUNT(*) c FROM "
             "execution_calibrations WHERE decision_id=?"),
            ("order rows", "SELECT COUNT(*) c FROM orders WHERE "
             "decision_id=? OR decision_id=?||'_x'"),
        ):
            row = await db.fetchone(sql, (decision, decision)
                                    if "||" in sql else (decision,))
            say(f"  {label:<24}: {row['c']}")
        journal_row = await db.fetchone(
            "SELECT status, realized_pnl FROM trade_journal "
            "WHERE decision_id=?", (decision,))
        say(f"  journal status          : {journal_row['status']} "
            f"(realized {journal_row['realized_pnl']:+.2f})")
        say("")
        say("  Lifecycle complete. Execution Quality, Counterfactual Lab "
            "and the journal now have real rows for this decision.")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--direction", default="bullish",
                    choices=["bullish", "bearish"])
    ap.add_argument("--hold-seconds", type=int, default=90)
    ap.add_argument("--execute", action="store_true",
                    help="actually submit; default is a dry run")
    ap.add_argument("--skip-close", action="store_true",
                    help="open only; leave the position journaled and "
                         "monitor-restorable")
    try:
        return asyncio.run(run(ap.parse_args()))
    except KeyboardInterrupt:
        say("\nInterrupted. Check scripts/close_all.py if an order was live.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
