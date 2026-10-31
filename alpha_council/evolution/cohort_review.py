"""
Alpha Council v2.6 - the ETF cohort review (hackathon review idea #5).

The 2026-10-30 universe expansion added an ETF cohort on a three-week
probation: trade them, prior them, Brier-score the priors, then decide
from evidence whether to expand, keep, or cut. This module writes that
evidence as one markdown report, deterministically - every number is SQL
over the desk's own records, so the operator reviews facts, not vibes.

The scheduler writes it once on/after priors.cohort_review_date (run-once
via system_state, restart-safe); scripts/etf_cohort_review.py renders the
same report on demand any day.

Place at: alpha_council/evolution/cohort_review.py
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from alpha_council.db.engine import Database
from alpha_council.utils.time import to_et, utc_now

BRIER_BASELINE = 0.25   # the score of always answering 50/50


async def _symbol_block(db: Database, symbol: str, since: str
                        ) -> dict[str, Any]:
    out: dict[str, Any] = {"symbol": symbol}

    out["candidates"] = await db.fetchone(
        "SELECT COUNT(*) AS n, ROUND(AVG(final_opportunity_score), 1) AS avg_score, "
        "ROUND(MAX(final_opportunity_score), 1) AS best_score "
        "FROM candidate_scores WHERE symbol=? AND created_at >= ?",
        (symbol, since)) or {}

    out["councils"] = await db.fetchone(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN state='FILLED' THEN 1 ELSE 0 END) AS filled "
        "FROM decisions WHERE symbol=? AND created_at >= ? "
        "AND COALESCE(candidate_track,'') != 'CALIBRATION'",
        (symbol, since)) or {}

    out["trades"] = await db.fetchone(
        "SELECT COUNT(*) AS n, ROUND(SUM(t.realized_pnl), 2) AS pnl, "
        "SUM(CASE WHEN t.realized_pnl > 0 THEN 1 ELSE 0 END) AS wins "
        "FROM trade_journal t JOIN decisions d ON d.decision_id=t.decision_id "
        "WHERE d.symbol=? AND t.status='CLOSED' AND t.closed_at >= ?",
        (symbol, since)) or {}

    out["top_gates"] = await db.fetchall(
        "SELECT stage, gate_id, COUNT(*) AS n FROM gate_rejections "
        "WHERE symbol=? AND occurred_at >= ? "
        "GROUP BY stage, gate_id ORDER BY n DESC LIMIT 4", (symbol, since))

    out["priors"] = await db.fetchone(
        "SELECT COUNT(*) AS computed, SUM(usable) AS usable, "
        "ROUND(AVG(CASE WHEN usable=1 THEN expected_move_pct END) * 100, 2) "
        "  AS avg_em_pct "
        "FROM market_priors WHERE symbol=? AND asof >= ?",
        (symbol, since)) or {}

    out["calibration"] = await db.fetchone(
        "SELECT COUNT(*) AS scored, ROUND(AVG(s.brier), 4) AS mean_brier, "
        "ROUND(AVG(s.within_expected_move) * 100, 1) AS within_em_pct "
        "FROM prior_scores s JOIN market_priors p ON p.prior_id=s.prior_id "
        "WHERE p.symbol=? AND p.asof >= ?", (symbol, since)) or {}
    return out


def _fmt_block(b: dict[str, Any]) -> list[str]:
    cand, cnc = b.get("candidates", {}), b.get("councils", {})
    trd, pri = b.get("trades", {}), b.get("priors", {})
    cal = b.get("calibration", {})
    lines = [f"### {b['symbol']}", ""]
    lines.append(
        f"- funnel: {int(cand.get('n') or 0)} candidate appearance(s)"
        + (f", best score {cand.get('best_score')}"
           if cand.get("best_score") is not None else "")
        + f"; councils {int(cnc.get('n') or 0)}"
        f" (filled {int(cnc.get('filled') or 0)})")
    n_tr = int(trd.get("n") or 0)
    if n_tr:
        lines.append(f"- closed trades: {n_tr}, P&L ${trd.get('pnl') or 0}, "
                     f"wins {int(trd.get('wins') or 0)}")
    else:
        lines.append("- closed trades: none")
    gates = b.get("top_gates") or []
    if gates:
        gate_text = ", ".join(f"{g['gate_id']} x{g['n']}" for g in gates)
        lines.append(f"- blocked most by: {gate_text}")
    computed = int(pri.get("computed") or 0)
    usable = int(pri.get("usable") or 0)
    if computed:
        lines.append(
            f"- chain priors: {computed} computed, {usable} usable"
            + (f", avg expected move {pri.get('avg_em_pct')}%"
               if pri.get("avg_em_pct") is not None else ""))
    else:
        lines.append("- chain priors: none computed")
    scored = int(cal.get("scored") or 0)
    if scored:
        lines.append(
            f"- calibration: {scored} scored, mean Brier "
            f"{cal.get('mean_brier')} (0.25 = always-50/50 baseline), "
            f"realized move within expected {cal.get('within_em_pct')}% "
            "of the time")
    lines.append("")
    return lines


async def build_cohort_review(db: Database, scoring: dict[str, Any],
                              now: datetime | None = None) -> str:
    """The full markdown report. Deterministic; safe to run any day."""
    now = now or utc_now()
    cfg = scoring.get("priors", {})
    cohort = [str(s).upper() for s in cfg.get("etf_cohort", [])]
    since = str(cfg.get("cohort_added") or "2026-10-30")

    lines = [
        f"# ETF Cohort Review - {to_et(now).date().isoformat()}",
        "",
        f"Cohort: {', '.join(cohort)} (observed since {since}).",
        "Every number below is SQL over the desk's own records: funnel "
        "appearances, council outcomes, closed P&L, the gates that blocked "
        "each symbol, and the Brier-scored chain priors. Use it to decide "
        "whether to expand the cohort, extend priors to single names, or "
        "cut symbols that never produced a tradable chain.",
        "",
        "## Reading guide",
        "",
        "- A symbol with candidate appearances but zero councils is losing "
        "to scoring floors or seat competition, not to liquidity.",
        "- A symbol whose priors are mostly unusable has a chain too thin "
        "to read - the same chain the options engine would have to trade. "
        "Those are the cut candidates.",
        "- Mean Brier meaningfully below 0.25 says the chain's crowd "
        "carried real directional information at this horizon; near 0.25 "
        "says it was (honestly) 50/50 and its value is the expected-move "
        "yardstick, not direction.",
        "",
        "## Symbols",
        "",
    ]
    for symbol in cohort:
        block = await _symbol_block(db, symbol, since)
        lines.extend(_fmt_block(block))

    lines.extend([
        "## Expansion decision checklist (operator)",
        "",
        "1. Cut: cohort symbols with zero usable priors AND zero "
        "structures - their chains cannot support the desk's spreads.",
        "2. Keep: symbols that reached councils or kept usable priors; "
        "they cost nothing beyond a few snapshots per scan.",
        "3. Expand priors to single names only if mean Brier beat the "
        "0.25 baseline or the expected-move yardstick measurably improved "
        "PM target realism (compare invalidation-exit rate on cohort vs "
        "non-cohort trades this window).",
        "4. Any universe addition follows the same probation: named "
        "cohort, dated review, measured verdict.",
        "",
        "*Generated deterministically by "
        "alpha_council/evolution/cohort_review.py; no model wrote any "
        "number in this file.*",
    ])
    return "\n".join(lines)


async def write_cohort_review(db: Database, scoring: dict[str, Any],
                              out_dir: str | Path = "reports",
                              now: datetime | None = None) -> Path:
    now = now or utc_now()
    text = await build_cohort_review(db, scoring, now=now)
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"ETF_COHORT_REVIEW_{to_et(now).date().isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    return path
