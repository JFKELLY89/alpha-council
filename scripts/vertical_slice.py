"""
Alpha Council v2.4 - vertical slice.

One symbol, all the way through: snapshot -> chain -> spread -> risk
constitution -> multi-leg paper order -> fill -> calibration -> close.
No LLM anywhere. This is the proof that the trading path works before the
Council is wired on top of it.

Defaults to --dry, which stops immediately before submission.

Place at: scripts/vertical_slice.py

Usage:
    uv run python scripts/vertical_slice.py                    # dry run
    uv run python scripts/vertical_slice.py --symbol SPY --live-paper
    uv run python scripts/vertical_slice.py --live-paper --close-after 120
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
from alpha_council.execution.order_manager import (  # noqa: E402
    OrderManager,
    walk_prices,
)
from alpha_council.models.enums import (  # noqa: E402
    CandidateTrack,
    DataConfidence,
    Direction,
    RiskDecision,
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
from alpha_council.utils.time import to_et, utc_now  # noqa: E402


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
    universe_cfg = load_yaml("universe")
    calendar = load_yaml("event_calendar")

    tier = args.tier
    tier_cfg = scoring.get("tiers", {}).get(tier, {})
    options_cfg = scoring.get("options", {})
    config_version = scoring.get("config_version", settings.config_version)
    decision = make_decision_id()

    rule("ALPHA COUNCIL - VERTICAL SLICE")
    say(f"  symbol   : {args.symbol}")
    say(f"  direction: {args.direction}")
    say(f"  tier     : {tier}")
    say(f"  decision : {decision}")
    say(f"  mode     : {'LIVE PAPER ORDER' if args.live_paper else 'DRY RUN'}")

    async with Database(settings.database_path) as db, \
            AlpacaRestClient(settings, scoring) as api:

        await ensure_config_version(db, config_version, scoring, risk_cfg,
                                    tier=tier, note="vertical slice")

        market = MarketDataService(api, db)
        chains = ChainService(api, market,
                              options_cfg.get("chain_cache_seconds", 60))
        orders = OrderManager(api, db, wait_seconds=args.wait)

        # ---- 1. account and clock ---------------------------------
        rule("1. ACCOUNT AND CLOCK")
        account = await api.get_account()
        clock = await api.get_clock()
        equity = float(account.get("equity", 0))
        market_open = bool(clock.get("is_open"))
        say(f"  account  : {account.get('account_number')}")
        say(f"  equity   : ${equity:,.2f}")
        say(f"  opt level: {account.get('options_trading_level')}")
        say(f"  open     : {market_open}")
        if not market_open and args.live_paper:
            say("  Market closed; a live order would not fill. Aborting.")
            return 1

        # ---- 2. underlying ----------------------------------------
        rule("2. UNDERLYING")
        snaps = await market.snapshots([args.symbol])
        snap = snaps.get(args.symbol)
        if snap is None or snap.mid is None:
            say("  No usable quote. Aborting.")
            return 1
        spot = snap.mid
        divergence = snap.internal_divergence()
        say(f"  mid        : {spot:.2f}")
        say(f"  last trade : {snap.last_trade}")
        say(f"  quote age  : {snap.quote_age}s")
        say(f"  spread     : {(snap.quote.spread_pct() or 0):.4%}")
        say(f"  divergence : {divergence:.4%}" if divergence is not None
            else "  divergence : n/a")

        eq_conf = DataConfidence.HIGH
        if (snap.quote_age or 999) > 120:
            eq_conf = DataConfidence.BLOCKED
        elif (snap.quote_age or 0) > 30:
            eq_conf = DataConfidence.DEGRADED
        say(f"  confidence : {eq_conf}")

        # ---- 3. chain ---------------------------------------------
        rule("3. OPTION CHAIN")
        cfilters = ChainFilters.from_tier(tier_cfg, options_cfg)
        chain = await chains.fetch(args.symbol, spot, cfilters)
        say(f"  contracts seen : {chain.contracts_seen}")
        say(f"  usable legs    : {chain.usable} "
            f"({len(chain.calls)} calls, {len(chain.puts)} puts)")
        say(f"  worst quote lag: {chain.max_quote_lag:.1f}s")
        say(f"  delta-adjusted : {chain.any_stale_adjusted}")
        if chain.rejections:
            say("  leg rejections:")
            for gate, n in sorted(chain.rejection_counts().items(),
                                  key=lambda x: -x[1]):
                say(f"    {gate:<30} {n}")
        if chain.usable < 2:
            say("  Not enough usable legs. Aborting.")
            return 1

        # ---- 4. spreads -------------------------------------------
        rule("4. SPREAD CONSTRUCTION")
        direction = (Direction.BULLISH if args.direction.upper() == "BULLISH"
                     else Direction.BEARISH)
        sfilters = SpreadFilters.from_config(tier_cfg, options_cfg)
        builder = SpreadBuilder(sfilters, scoring.get("structure_weights"),
                                scoring.get("leg_liquidity_weights"))
        spreads = builder.build(chain, direction,
                                max_debit_allowed=equity * 0.02 / 100.0)

        say(f"  combinations tried : {spreads.combinations_tried}")
        say(f"  structures returned: {len(spreads.structures)}")
        if spreads.rejections:
            for gate, n in sorted(spreads.rejection_counts().items(),
                                  key=lambda x: -x[1])[:6]:
                say(f"    {gate:<30} {n}")
        if not spreads.structures:
            say("  No valid structure. Aborting.")
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

        selected = spreads.structures[args.rank - 1]
        say("")
        say(f"  selected rank {selected.rank}: "
            f"{selected.long_leg.symbol} / {selected.short_leg.symbol}")
        say(f"  max loss/spread ${selected.max_loss_per_spread:.2f}  "
            f"max profit ${selected.max_profit_per_spread:.2f}  "
            f"breakeven {selected.breakeven:.2f}")
        say(f"  staleness buffer ${selected.staleness_buffer:.2f}")

        # ---- 5. risk ----------------------------------------------
        rule("5. RISK CONSTITUTION")
        positions = await orders.open_option_positions()
        open_risk = sum(abs(float(p.get("cost_basis", 0))) for p in positions)
        portfolio = PortfolioState(
            equity=equity,
            day_start_equity=float(account.get("last_equity", equity)),
            peak_equity=max(equity, float(account.get("last_equity", equity))),
            open_risk_dollars=open_risk,
            open_position_count=len(positions),
        )
        constitution = RiskConstitution(risk_cfg, scoring,
                                        load_blackouts(calendar))
        request = TradeRequest(
            decision_id=decision, symbol=args.symbol,
            sector=sector_of(args.symbol, universe_cfg.get("sectors", {})),
            direction=direction, structure=selected,
            desired_risk_pct=args.risk_pct,
            pm_confidence=args.confidence,
            red_team_verdict=Verdict.PASS, red_team_max_risk_pct=None,
            equity_data_confidence=eq_conf,
            option_data_confidence=DataConfidence.HIGH,
            final_opportunity_score=args.score,
            market_open=market_open,
            is_calibration_trade=True,
        )
        evaluation = constitution.evaluate(request, portfolio, tier=tier,
                                           config_version=config_version)

        say(f"  decision     : {evaluation.decision}")
        say(f"  requested qty: {evaluation.requested_qty}")
        say(f"  approved qty : {evaluation.approved_qty}")
        say(f"  approved risk: ${evaluation.approved_max_loss:,.2f}")
        say(f"  open risk pct: {evaluation.total_open_risk_pct_after:.2f}%")
        if evaluation.violations:
            say("  violations:")
            for v in evaluation.violations:
                say(f"    [{v.severity}] {v.rule_id}: {v.message}")

        if evaluation.decision in (RiskDecision.REJECT, RiskDecision.HALT):
            say("  Blocked by the Risk Constitution. Stopping.")
            return 0

        qty = min(evaluation.approved_qty, args.max_qty)
        max_debit = evaluation.approved_max_loss / max(1, qty) / 100.0

        # ---- 6. limit walk plan -----------------------------------
        rule("6. LIMIT WALK PLAN")
        prices = walk_prices(selected.adjusted_mid_debit,
                             selected.natural_debit,
                             selected.staleness_buffer, max_debit)
        say(f"  adjusted mid : {selected.adjusted_mid_debit:.2f}")
        say(f"  natural      : {selected.natural_debit:.2f}")
        say(f"  ceiling      : {max_debit:.2f}")
        for i, p in enumerate(prices, start=1):
            say(f"  attempt {i}    : ${p:.2f} "
                f"(risk ${p * 100 * qty:,.2f} for {qty})")

        if not args.live_paper:
            rule("DRY RUN COMPLETE")
            say("  Everything up to submission works.")
            say("  Re-run with --live-paper during RTH to place the order.")
            say(f"  client: {api.stats()}  chains: {chains.stats()}")
            return 0

        # ---- 7. submit --------------------------------------------
        rule("7. SUBMITTING PAPER ORDER")
        say(f"  {qty} x {selected.strategy} {args.symbol} "
            f"{selected.long_leg.strike:g}/{selected.short_leg.strike:g} "
            f"exp {selected.expiration}")
        outcome = await orders.execute_with_walk(
            selected, decision, qty, max_debit)

        for step in outcome.steps:
            say(f"  attempt {step.attempt}: ${step.limit_debit:.2f} "
                f"-> {step.status}"
                + (f" @ ${step.fill_price:.2f}" if step.fill_price else ""))

        rule("8. OUTCOME")
        say(f"  status  : {outcome.final_status}")
        say(f"  filled  : {outcome.filled}")
        if outcome.filled:
            say(f"  debit   : ${outcome.fill_debit:.2f}")
            say(f"  seconds : {outcome.seconds_to_fill:.1f}")
            say(f"  order   : {outcome.order_id}")

            fresh = await market.snapshots([args.symbol])
            underlying_at_fill = (fresh.get(args.symbol).mid
                                  if fresh.get(args.symbol) else None)
            record = await orders.record_calibration(
                outcome, selected, CandidateTrack.CALIBRATION, direction,
                underlying_at_submit=spot,
                underlying_at_fill=underlying_at_fill)
            if record:
                rule("9. EXECUTION CALIBRATION")
                say(f"  indicative adjusted mid : ${record.indicative_adjusted_mid:.2f}")
                say(f"  actual fill             : ${record.actual_fill_debit:.2f}")
                say(f"  bias vs adjusted        : ${record.fill_bias_vs_adjusted:+.2f}")
                say(f"  bias vs initial limit   : ${record.fill_bias_vs_limit:+.2f}")
                say(f"  slippage                : {record.fill_slippage_pct:+.2%}")
                say(f"  walk steps              : {record.limit_walk_steps}")
                say("")
                say("  This is the first measurement of what the free")
                say("  Indicative feed costs against real fills.")

            if args.close_after > 0:
                rule(f"10. CLOSING IN {args.close_after}s")
                await asyncio.sleep(args.close_after)
                close_chain = await chains.fetch(args.symbol, spot, cfilters)
                close_spreads = builder.build(close_chain, direction,
                                              max_debit_allowed=max_debit)
                close_struct = next(
                    (s for s in close_spreads.structures
                     if s.long_leg.strike == selected.long_leg.strike
                     and s.short_leg.strike == selected.short_leg.strike),
                    selected)
                close_out = await orders.execute_with_walk(
                    close_struct, decision + "_c", qty,
                    max_allowed_debit=close_struct.natural_debit, closing=True)
                say(f"  close status: {close_out.final_status}")
                if close_out.filled:
                    pnl = (close_out.fill_debit - outcome.fill_debit) * 100 * qty
                    say(f"  exit credit : ${close_out.fill_debit:.2f}")
                    say(f"  realized P&L: ${pnl:+,.2f}")
        else:
            say("  No fill after the full walk. Recorded as NO_FILL.")

        say("")
        say(f"  client: {api.stats()}  chains: {chains.stats()}")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--direction", default="BULLISH",
                    choices=["BULLISH", "BEARISH"])
    ap.add_argument("--tier", type=int, default=1, choices=[1, 2, 3])
    ap.add_argument("--rank", type=int, default=1)
    ap.add_argument("--risk-pct", type=float, default=0.25,
                    help="deliberately small for a calibration trade")
    ap.add_argument("--max-qty", type=int, default=1)
    ap.add_argument("--confidence", type=float, default=0.75)
    ap.add_argument("--score", type=float, default=75.0)
    ap.add_argument("--wait", type=float, default=30.0)
    ap.add_argument("--close-after", type=int, default=0)
    ap.add_argument("--live-paper", action="store_true",
                    help="actually submit; omit for a dry run")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
