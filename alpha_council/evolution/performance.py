"""
Alpha Council v2.5 §20 - deterministic strategy performance.

Everything here is SQL and arithmetic over the system's own records. No
model computes a metric, and the challenger comparison names what it
could not measure instead of hiding it.

The comparison method (common-set, stated in the review doc):

  Champion performance is realized: closed trades in trade_journal.

  Challenger performance is evaluated over its shadow decisions:
    - champion traded, challenger would trade  -> same realized outcome
    - champion traded, challenger would pass   -> zero (the avoided trade)
    - champion passed,  challenger would pass  -> zero (identical)
    - champion passed,  challenger would trade -> the marked rejected
      shadow when one exists, else UNMEASURED and counted as such.

  Marked-to-model rows (rejected shadows still open) use the last mark.
  A comparison whose unmeasured share is high is flagged by the promotion
  rules, not smoothed over.

Place at: alpha_council/evolution/performance.py
"""

from __future__ import annotations

import json
from typing import Any

from alpha_council.db.engine import Database
from alpha_council.models.evolution import StrategyPerformance
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.time import iso_utc, utc_now


async def champion_performance(db: Database,
                               strategy_id: str) -> StrategyPerformance:
    """Realized performance from the trade journal."""
    rows = await db.fetchall(
        "SELECT t.realized_pnl, t.candidate_track, t.closed_at "
        "FROM trade_journal t WHERE t.status='CLOSED' ORDER BY t.closed_at")
    pnls = [float(r["realized_pnl"] or 0.0) for r in rows]
    decisions = await db.fetchone("SELECT COUNT(*) AS n FROM decisions")

    equity_base = float(await db.get_state("peak_equity", 100_000.0) or
                        100_000.0)
    total = round(sum(pnls), 2)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = (len(wins) / len(pnls)) if pnls else None
    avg_win = round(sum(wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(losses) / len(losses), 2) if losses else None

    execution = await db.fetchone(
        "SELECT AVG(fill_bias_vs_adjusted) AS mean_bias FROM "
        "execution_calibrations WHERE actual_fill_debit IS NOT NULL")

    by_track: dict[str, float] = {}
    for r in rows:
        track = (r.get("candidate_track") or "MOMENTUM").upper()
        by_track[track] = by_track.get(track, 0.0) \
            + float(r["realized_pnl"] or 0.0)

    return StrategyPerformance(
        strategy_id=strategy_id,
        observations=int((decisions or {}).get("n") or 0),
        closed_trades=len(pnls),
        total_pnl=total,
        return_pct=round(total / equity_base * 100, 4) if equity_base else 0.0,
        win_rate=round(win_rate, 4) if win_rate is not None else None,
        expectancy=(round(win_rate * (avg_win or 0.0)
                          + (1 - win_rate) * (avg_loss or 0.0), 2)
                    if win_rate is not None else None),
        max_drawdown_pct=_max_drawdown_pct(pnls, equity_base),
        average_win=avg_win,
        average_loss=avg_loss,
        profit_factor=(round(sum(wins) / abs(sum(losses)), 3)
                       if losses and sum(losses) < 0 and wins else None),
        event_pnl=round(by_track.get("EVENT", 0.0), 2),
        momentum_pnl=round(by_track.get("MOMENTUM", 0.0), 2),
        execution_bias_mean=(round(float(execution["mean_bias"]), 4)
                             if execution and execution.get("mean_bias")
                             is not None else None),
    )


async def challenger_performance(db: Database,
                                 strategy_id: str) -> StrategyPerformance:
    """Common-set hypothetical performance from shadow decisions."""
    rows = await db.fetchall(
        "SELECT s.source_decision_id, s.would_trade, s.rationale_json, "
        "t.realized_pnl, t.qty, t.candidate_track AS journal_track "
        "FROM strategy_shadow_decisions s "
        "LEFT JOIN trade_journal t "
        "  ON t.decision_id = s.source_decision_id AND t.status='CLOSED' "
        "WHERE s.strategy_id=?", (strategy_id,))

    pnls: list[float] = []
    by_track: dict[str, float] = {}
    unmeasured = 0

    for r in rows:
        would = bool(r["would_trade"])
        try:
            rationale = json.loads(r["rationale_json"] or "{}")
        except (ValueError, TypeError):
            rationale = {}
        track = str(rationale.get("track")
                    or r.get("journal_track") or "MOMENTUM").upper()

        if r.get("realized_pnl") is not None:
            pnl = float(r["realized_pnl"]) if would else 0.0
            pnls.append(pnl)
            by_track[track] = by_track.get(track, 0.0) + pnl
            continue

        if not would:
            pnls.append(0.0)          # both passed: identical, zero
            continue

        # Challenger-only trade: measurable only through a marked
        # rejected shadow on the same decision.
        source = r["source_decision_id"]
        shadow = None
        if source and not source.startswith("cand:"):
            shadow = await db.fetchone(
                "SELECT r.final_pnl_per_spread, r.last_mark_debit, "
                "r.entry_reference_debit FROM rejected_shadows r "
                "JOIN gate_rejections g ON g.rejection_id = r.rejection_id "
                "WHERE g.decision_id=? ORDER BY r.entry_timestamp LIMIT 1",
                (source,))
        if shadow and shadow.get("final_pnl_per_spread") is not None:
            pnl = float(shadow["final_pnl_per_spread"])
        elif shadow and shadow.get("last_mark_debit") is not None:
            pnl = round((float(shadow["last_mark_debit"])
                         - float(shadow["entry_reference_debit"])) * 100, 2)
        else:
            unmeasured += 1
            continue
        pnls.append(pnl)
        by_track[track] = by_track.get(track, 0.0) + pnl

    equity_base = float(await db.get_state("peak_equity", 100_000.0) or
                        100_000.0)
    total = round(sum(pnls), 2)
    traded = [p for p in pnls if p != 0.0]
    wins = [p for p in traded if p > 0]
    losses = [p for p in traded if p < 0]
    win_rate = (len(wins) / len(traded)) if traded else None

    return StrategyPerformance(
        strategy_id=strategy_id,
        observations=len(rows),
        closed_trades=len(traded),
        total_pnl=total,
        return_pct=round(total / equity_base * 100, 4) if equity_base else 0.0,
        win_rate=round(win_rate, 4) if win_rate is not None else None,
        expectancy=(round(sum(traded) / len(traded), 2) if traded else None),
        max_drawdown_pct=_max_drawdown_pct(pnls, equity_base),
        average_win=round(sum(wins) / len(wins), 2) if wins else None,
        average_loss=round(sum(losses) / len(losses), 2) if losses else None,
        profit_factor=(round(sum(wins) / abs(sum(losses)), 3)
                       if losses and wins else None),
        event_pnl=round(by_track.get("EVENT", 0.0), 2),
        momentum_pnl=round(by_track.get("MOMENTUM", 0.0), 2),
        unmeasured_observations=unmeasured,
    )


def _max_drawdown_pct(pnls: list[float], equity_base: float) -> float:
    """Peak-to-trough of the cumulative realized P&L, as % of equity."""
    if not pnls or equity_base <= 0:
        return 0.0
    cumulative = peak = drawdown = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return round(drawdown / equity_base * 100, 4)


async def snapshot(db: Database, perf: StrategyPerformance) -> None:
    await db.execute(
        "INSERT INTO strategy_performance_snapshots(snapshot_id, "
        "strategy_id, as_of, observations, closed_trades, total_pnl, "
        "return_pct, win_rate, expectancy, max_drawdown_pct, average_win, "
        "average_loss, profit_factor, event_pnl, momentum_pnl, "
        "execution_bias_mean, execution_bias_median, metrics_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"sps_{new_uuid()[:12]}", perf.strategy_id, iso_utc(utc_now()),
         perf.observations, perf.closed_trades, perf.total_pnl,
         perf.return_pct, perf.win_rate, perf.expectancy,
         perf.max_drawdown_pct, perf.average_win, perf.average_loss,
         perf.profit_factor, perf.event_pnl, perf.momentum_pnl,
         perf.execution_bias_mean, perf.execution_bias_median,
         json.dumps({"unmeasured_observations":
                     perf.unmeasured_observations})))
