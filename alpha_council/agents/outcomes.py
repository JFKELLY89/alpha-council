"""
Alpha Council v2.6 - recent-outcome feedback for the council.

Hackathon review 2026-10-21, idea #2 (from Autobelay, 1st place): the PM
judged every proposal with zero knowledge of how its earlier proposals
ended, and symbols the desk had already refused re-consumed council seats
the same session. Two deterministic SQL blocks close that loop:

  RECENT OUTCOMES. The last N closed trades plus a rolling-window
  aggregate and a per-exit-reason breakdown, computed in the database and
  handed to the PM / Red Team as evidence. The model interprets the
  numbers; it never computes them.

  SYMBOL SESSION HISTORY. Today's gate refusals and abstains for the one
  symbol on the table, so a thesis the Risk Constitution refused at 10:15
  is not re-argued verbatim at 11:00.

Reporting, not policy: nothing in this module blocks a trade. The
deterministic repeat-rejection cooldown lives in the scheduler's seat
allocation, where a skip is logged and auditable.

Place at: alpha_council/agents/outcomes.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from alpha_council.db.engine import Database
from alpha_council.utils.time import iso_utc, to_et, utc_now

# The whole block must stay small: it rides inside a 6k-token evidence
# package whose load-bearing sections (structures, payoffs) come first.
OUTCOME_SAMPLE_LIMIT = 10

OUTCOMES_NOTE = (
    "The system's own recent closed trades, computed in SQL from the "
    "journal. Use them to avoid repeating structural mistakes - theses "
    "that invalidated within hours, targets the holding period never "
    "delivered. Context, not a mood: do not abstain merely because losses "
    "appear here, and do not chase merely because wins do."
)

HISTORY_NOTE = (
    "This symbol was already evaluated today and refused or abstained at "
    "the recorded stage. Propose again only if the evidence has materially "
    "changed since; restating an unchanged thesis wastes the council."
)


def _hold_hours(opened_at: Any, closed_at: Any) -> float | None:
    try:
        opened = datetime.fromisoformat(str(opened_at))
        closed = datetime.fromisoformat(str(closed_at))
    except (TypeError, ValueError):
        return None
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    if closed.tzinfo is None:
        closed = closed.replace(tzinfo=timezone.utc)
    return round((closed - opened).total_seconds() / 3600.0, 1)


async def recent_outcomes(db: Database, limit: int = OUTCOME_SAMPLE_LIMIT,
                          window_days: int = 30,
                          now: datetime | None = None
                          ) -> dict[str, Any] | None:
    """Portfolio-level outcome block for the PM / Red Team packages.

    Returns None when the journal holds no closed trades in the window, so
    an empty desk ships no empty section.
    """
    now = now or utc_now()
    since = iso_utc(now - timedelta(days=window_days))

    rows = await db.fetchall(
        "SELECT d.symbol, t.candidate_track, t.exit_reason, t.realized_pnl, "
        "t.opened_at, t.closed_at "
        "FROM trade_journal t "
        "JOIN decisions d ON d.decision_id = t.decision_id "
        "WHERE t.status = 'CLOSED' AND t.closed_at >= ? "
        "ORDER BY t.closed_at DESC LIMIT ?", (since, limit))
    if not rows:
        return None

    sample = [{
        "symbol": r["symbol"],
        "track": r["candidate_track"],
        "exit_reason": r["exit_reason"],
        "pnl": round(float(r["realized_pnl"] or 0.0), 2),
        "hold_hours": _hold_hours(r["opened_at"], r["closed_at"]),
    } for r in rows]

    agg = await db.fetchone(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins, "
        "ROUND(SUM(realized_pnl), 2) AS pnl, "
        "ROUND(AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 2) "
        "  AS average_win, "
        "ROUND(AVG(CASE WHEN realized_pnl <= 0 THEN realized_pnl END), 2) "
        "  AS average_loss "
        "FROM trade_journal WHERE status = 'CLOSED' AND closed_at >= ?",
        (since,)) or {}
    n = int(agg.get("n") or 0)

    by_reason = await db.fetchall(
        "SELECT exit_reason, COUNT(*) AS n, ROUND(SUM(realized_pnl), 2) AS pnl "
        "FROM trade_journal WHERE status = 'CLOSED' AND closed_at >= ? "
        "GROUP BY exit_reason ORDER BY n DESC", (since,))

    return {
        "window_days": window_days,
        "last_closed_trades": sample,
        "aggregate": {
            "closed_trades": n,
            "win_rate": round(int(agg.get("wins") or 0) / n, 3) if n else None,
            "total_pnl": agg.get("pnl"),
            "average_win": agg.get("average_win"),
            "average_loss": agg.get("average_loss"),
        },
        "by_exit_reason": by_reason,
        "note": OUTCOMES_NOTE,
    }


async def symbol_session_history(db: Database, symbol: str,
                                 now: datetime | None = None
                                 ) -> dict[str, Any] | None:
    """Today's refusals and abstains for one symbol, or None if clean."""
    now = now or utc_now()
    day_start = to_et(now).replace(hour=0, minute=0, second=0, microsecond=0)
    since = day_start.astimezone(timezone.utc).isoformat(
        timespec="microseconds")

    rejections = await db.fetchall(
        "SELECT stage, gate_id, COUNT(*) AS n, "
        "substr(MAX(occurred_at), 12, 5) AS last_utc, "
        "substr(MAX(note), 1, 160) AS note "
        "FROM gate_rejections "
        "WHERE symbol = ? AND occurred_at >= ? "
        "AND stage IN ('PM_ABSTAIN', 'RED_TEAM', 'RISK', 'EXECUTION') "
        "GROUP BY stage, gate_id ORDER BY n DESC", (symbol, since))

    abstains = await db.fetchall(
        "SELECT substr(p.created_at, 12, 5) AS at_utc, "
        "substr(p.abstain_reason, 1, 160) AS reason "
        "FROM trade_proposals p "
        "JOIN decisions d ON d.decision_id = p.decision_id "
        "WHERE d.symbol = ? AND p.trade = 0 AND p.created_at >= ? "
        "ORDER BY p.created_at DESC LIMIT 3", (symbol, since))

    if not rejections and not abstains:
        return None
    return {
        "symbol": symbol,
        "todays_refusals": rejections,
        "todays_abstains": abstains,
        "note": HISTORY_NOTE,
    }
