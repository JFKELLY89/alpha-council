"""
Alpha Council v2.7 - rhetoric audits (hackathon review idea #4, from
Autobelay: "every percentage the model cites is matched to the prior it
was given; exit reasons are graded against account facts").

Operator-selected scope (2026-10-30, the recommended defaults):

  INVALIDATION CITATIONS. Every numeric threshold the PM wrote into an
  invalidation rule is matched against the NAMED levels that decision's
  own recorded evidence actually contained - spot, strikes, breakeven,
  scenario bounds, the prior's expected-move band. Structured fields
  only; no prose parsing, which is where citation matchers go to cry
  wolf.

  EXIT CLAIMS. Every recorded exit reason on a closed trade is graded
  against market facts: did the underlying actually cross the level the
  reason names, was the time stop actually due, did the exit fire inside
  regular hours (the 2026-09-17 after-hours loop would have produced an
  INCONSISTENT row here on day one).

Grades for citations: SUPPORTED (within tolerance of a named level),
DERIVED (inside the envelope the levels span - a judgment call, not a
hallucination), UNSUPPORTED (outside everything the model was shown),
NOT_CHECKABLE. For exits: CONSISTENT / INCONSISTENT / NOT_CHECKABLE.

REPORTING-ONLY BY DESIGN: nothing here blocks, resizes, or re-reviews a
trade. Findings land in rhetoric_audits and the nightly log; what to do
about a repeat offender is an operator decision the data will inform.

Place at: alpha_council/audit/rhetoric.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from alpha_council.db.engine import Database
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.time import is_rth, iso_utc, to_et, utc_now

RHETORIC_DDL = (
    """
    CREATE TABLE IF NOT EXISTS rhetoric_audits (
        audit_id     TEXT PRIMARY KEY,
        decision_id  TEXT NOT NULL,
        audited_at   TEXT NOT NULL,
        audit_type   TEXT NOT NULL
            CHECK(audit_type IN ('INVALIDATION_CITE','EXIT_CLAIM')),
        subject      TEXT NOT NULL,
        claim_value  REAL,
        grade        TEXT NOT NULL CHECK(grade IN (
            'SUPPORTED','DERIVED','UNSUPPORTED',
            'CONSISTENT','INCONSISTENT','NOT_CHECKABLE')),
        detail       TEXT NOT NULL,
        context_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(decision_id, audit_type, subject)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_rhetoric_grade
        ON rhetoric_audits(grade, audited_at DESC)
    """,
)

CITE_GRADES_BAD = ("UNSUPPORTED",)
EXIT_GRADES_BAD = ("INCONSISTENT",)


async def ensure_schema(db: Database) -> None:
    for ddl in RHETORIC_DDL:
        await db.execute(ddl)


# ----------------------------------------------------------------------
# pure grading - testable without a database
# ----------------------------------------------------------------------

@dataclass(slots=True)
class Finding:
    audit_type: str
    subject: str
    claim_value: float | None
    grade: str
    detail: str
    context: dict[str, Any]


# A legitimate stop sits BELOW every named level by design (support is
# not in the named set), so the envelope carries slack scaled off spot:
# a level within 15% of spot is a judgment call (DERIVED); beyond it the
# model named a price nothing it was shown comes near (UNSUPPORTED).
SPOT_SLACK_REL = 0.15


def grade_level_claim(claim: float, named_levels: dict[str, float],
                      tolerance_rel: float = 0.0075) -> tuple[str, str]:
    """(grade, detail) for one numeric price-level claim.

    SUPPORTED: within tolerance of a level the evidence actually named.
    DERIVED: inside the envelope the named levels span, widened around
    spot by SPOT_SLACK_REL - interpolations and ordinary stop placement,
    which the tolerant posture accepts.
    UNSUPPORTED: outside everything the model was shown - an invented
    level.
    """
    levels = {k: v for k, v in named_levels.items()
              if isinstance(v, (int, float)) and v > 0}
    if not levels or claim is None or claim <= 0:
        return ("NOT_CHECKABLE", "no named levels recorded for this decision")

    nearest = min(levels.items(), key=lambda kv: abs(kv[1] - claim))
    rel = abs(nearest[1] - claim) / max(abs(nearest[1]), 1e-9)
    if rel <= tolerance_rel:
        return ("SUPPORTED",
                f"matches {nearest[0]} {nearest[1]:.2f} (within "
                f"{rel:.2%})")

    bounds = list(levels.values())
    spot = levels.get("spot")
    if spot:
        bounds.extend([spot * (1 - SPOT_SLACK_REL),
                       spot * (1 + SPOT_SLACK_REL)])
    low = min(bounds) * (1 - tolerance_rel)
    high = max(bounds) * (1 + tolerance_rel)
    if low <= claim <= high:
        return ("DERIVED",
                f"inside the evidence envelope [{low:.2f}, {high:.2f}]; "
                f"nearest named level {nearest[0]} {nearest[1]:.2f}")
    return ("UNSUPPORTED",
            f"outside the evidence envelope [{low:.2f}, {high:.2f}]; "
            f"nearest named level {nearest[0]} {nearest[1]:.2f} "
            f"({rel:.1%} away)")


def grade_time_claim(days: float, horizon_days: int | None) -> tuple[str, str]:
    if days is None or days <= 0:
        return ("NOT_CHECKABLE", "no numeric time threshold")
    if horizon_days and days <= horizon_days:
        return ("SUPPORTED", f"within the stated {horizon_days}d horizon")
    if days <= 15:
        return ("DERIVED", "inside the 15d system maximum but beyond the "
                           "stated horizon")
    return ("UNSUPPORTED", "beyond the 15d system maximum")


# ----------------------------------------------------------------------
# evidence levels - from the decision's own recorded rows
# ----------------------------------------------------------------------

async def named_levels_for(db: Database, decision_id: str,
                           symbol: str) -> dict[str, float]:
    """Price levels the decision's recorded evidence actually contained."""
    levels: dict[str, float] = {}
    structures = await db.fetchall(
        "SELECT rank, long_strike, short_strike, breakeven, underlying_price "
        "FROM option_structures WHERE decision_id=?", (decision_id,))
    for s in structures:
        rank = s["rank"]
        levels[f"long_strike_r{rank}"] = float(s["long_strike"] or 0)
        levels[f"short_strike_r{rank}"] = float(s["short_strike"] or 0)
        levels[f"breakeven_r{rank}"] = float(s["breakeven"] or 0)
        if s["underlying_price"]:
            levels["spot"] = float(s["underlying_price"])

    payoffs = await db.fetchall(
        "SELECT underlying_low, underlying_high FROM scenario_payoffs "
        "WHERE decision_id=?", (decision_id,))
    if payoffs:
        levels["scenario_low"] = min(float(p["underlying_low"] or 0)
                                     for p in payoffs)
        levels["scenario_high"] = max(float(p["underlying_high"] or 0)
                                      for p in payoffs)

    spot = levels.get("spot")
    if spot:
        prior = await db.fetchone(
            "SELECT expected_move_pct FROM market_priors WHERE symbol=? "
            "AND usable=1 ORDER BY asof DESC LIMIT 1", (symbol,))
        if prior and prior.get("expected_move_pct"):
            em = float(prior["expected_move_pct"])
            levels["prior_band_low"] = spot * (1 - em)
            levels["prior_band_high"] = spot * (1 + em)
    return levels


# ----------------------------------------------------------------------
# the two auditors
# ----------------------------------------------------------------------

async def audit_decision_citations(db: Database, decision: dict[str, Any],
                                   tolerance_rel: float) -> list[Finding]:
    findings: list[Finding] = []
    proposals = await db.fetchall(
        "SELECT revision, invalidation_json, expected_horizon_days "
        "FROM trade_proposals WHERE decision_id=? AND trade=1",
        (decision["decision_id"],))
    if not proposals:
        return findings
    levels = await named_levels_for(db, decision["decision_id"],
                                    decision["symbol"])
    for proposal in proposals:
        try:
            rules = json.loads(proposal["invalidation_json"] or "[]")
        except (TypeError, ValueError):
            continue
        for index, rule in enumerate(rules):
            threshold = rule.get("threshold")
            rule_type = str(rule.get("rule_type") or "")
            if threshold is None:
                continue
            subject = (f"r{proposal['revision']}_rule{index}:"
                       f"{rule_type} {rule.get('comparator')} {threshold}")
            if rule_type in ("PRICE", "VWAP"):
                grade, detail = grade_level_claim(float(threshold), levels,
                                                  tolerance_rel)
            elif rule_type == "TIME":
                grade, detail = grade_time_claim(
                    float(threshold), proposal.get("expected_horizon_days"))
            else:
                continue
            findings.append(Finding(
                "INVALIDATION_CITE", subject, float(threshold), grade,
                detail, {"description": str(rule.get("description"))[:160],
                         "levels": {k: round(v, 2)
                                    for k, v in levels.items()}}))
    return findings


async def _bar_window(db: Database, symbol: str, start_iso: str,
                      end_iso: str) -> list[dict[str, Any]]:
    return await db.fetchall(
        "SELECT ts, high, low, close FROM market_bars "
        "WHERE symbol=? AND timeframe='5Min' AND ts BETWEEN ? AND ? "
        "ORDER BY ts", (symbol, start_iso, end_iso))


async def audit_closed_trade(db: Database, trade: dict[str, Any],
                             tolerance_rel: float) -> list[Finding]:
    """Grade the recorded exit reason against market facts."""
    findings: list[Finding] = []
    reason = str(trade.get("exit_reason") or "")
    if reason in ("", "MANUAL", "COMPETITION_FLATTEN", "RISK_HALT"):
        return findings
    decision_id = trade["decision_id"]
    symbol = trade["symbol"]
    closed_at = trade.get("closed_at")
    if not closed_at:
        return findings
    closed_dt = datetime.fromisoformat(str(closed_at))
    if closed_dt.tzinfo is None:
        closed_dt = closed_dt.replace(tzinfo=timezone.utc)

    # Every non-manual exit must have fired inside regular hours. The
    # 2026-09-17 after-hours invalidation loop is exactly this row.
    if not is_rth(closed_dt):
        findings.append(Finding(
            "EXIT_CLAIM", f"exit_rth:{reason}", None, "INCONSISTENT",
            f"exit recorded at {to_et(closed_dt):%H:%M} ET, outside "
            "regular hours", {}))
    else:
        findings.append(Finding(
            "EXIT_CLAIM", f"exit_rth:{reason}", None, "CONSISTENT",
            f"exit inside RTH ({to_et(closed_dt):%H:%M} ET)", {}))

    window_start = iso_utc(closed_dt - timedelta(minutes=90))
    window_end = iso_utc(closed_dt + timedelta(minutes=10))
    bars = await _bar_window(db, symbol, window_start, window_end)

    if reason == "UNDERLYING_INVALIDATION":
        try:
            rules = json.loads(trade.get("invalidation_json") or "[]")
        except (TypeError, ValueError):
            rules = []
        thresholds = [float(r["threshold"]) for r in rules
                      if r.get("threshold") is not None
                      and r.get("rule_type") in ("PRICE", "VWAP")]
        if not bars or not thresholds:
            findings.append(Finding(
                "EXIT_CLAIM", "exit_level:UNDERLYING_INVALIDATION", None,
                "NOT_CHECKABLE", "no bars or no numeric thresholds", {}))
        else:
            lows = min(float(b["low"]) for b in bars)
            highs = max(float(b["high"]) for b in bars)
            touched = any(
                (lows <= t <= highs)
                or abs(lows - t) / t <= tolerance_rel
                or abs(highs - t) / t <= tolerance_rel
                for t in thresholds)
            nearest = min(thresholds,
                          key=lambda t: min(abs(lows - t), abs(highs - t)))
            findings.append(Finding(
                "EXIT_CLAIM", "exit_level:UNDERLYING_INVALIDATION", nearest,
                "CONSISTENT" if touched else "INCONSISTENT",
                f"bars [{lows:.2f}, {highs:.2f}] vs threshold(s) "
                f"{[round(t, 2) for t in thresholds]}", {}))

    elif reason == "UNDERLYING_TARGET":
        structure = await db.fetchone(
            "SELECT o.short_strike FROM risk_evaluations r "
            "JOIN option_structures o ON o.structure_id = r.structure_id "
            "WHERE r.decision_id=? ORDER BY r.evaluated_at DESC LIMIT 1",
            (decision_id,))
        strike = float((structure or {}).get("short_strike") or 0)
        if not bars or not strike:
            findings.append(Finding(
                "EXIT_CLAIM", "exit_level:UNDERLYING_TARGET", strike or None,
                "NOT_CHECKABLE", "no bars or no recorded structure", {}))
        else:
            lows = min(float(b["low"]) for b in bars)
            highs = max(float(b["high"]) for b in bars)
            touched = (lows <= strike <= highs
                       or abs(highs - strike) / strike <= tolerance_rel
                       or abs(lows - strike) / strike <= tolerance_rel)
            findings.append(Finding(
                "EXIT_CLAIM", "exit_level:UNDERLYING_TARGET", strike,
                "CONSISTENT" if touched else "INCONSISTENT",
                f"bars [{lows:.2f}, {highs:.2f}] vs short strike "
                f"{strike:.2f}", {}))

    elif reason == "TIME_STOP":
        structure = await db.fetchone(
            "SELECT o.expiration FROM risk_evaluations r "
            "JOIN option_structures o ON o.structure_id = r.structure_id "
            "WHERE r.decision_id=? ORDER BY r.evaluated_at DESC LIMIT 1",
            (decision_id,))
        expiration = (structure or {}).get("expiration")
        if not expiration:
            findings.append(Finding(
                "EXIT_CLAIM", "exit_time:TIME_STOP", None, "NOT_CHECKABLE",
                "no recorded structure expiration", {}))
        else:
            exp = datetime.fromisoformat(str(expiration)).date()
            dte = (exp - to_et(closed_dt).date()).days
            findings.append(Finding(
                "EXIT_CLAIM", "exit_time:TIME_STOP", float(dte),
                "CONSISTENT" if dte <= 3 else "INCONSISTENT",
                f"{dte} DTE at close vs the 2-DTE floor", {}))

    elif reason in ("PROFIT_TARGET", "PREMIUM_STOP"):
        entry = float(trade.get("entry_debit") or 0)
        exit_credit = trade.get("exit_credit")
        if not entry or exit_credit is None:
            findings.append(Finding(
                "EXIT_CLAIM", f"exit_mark:{reason}", None, "NOT_CHECKABLE",
                "entry or exit mark missing", {}))
        else:
            ratio = float(exit_credit) / entry
            ok = ratio >= 1.0 if reason == "PROFIT_TARGET" else ratio <= 0.60
            findings.append(Finding(
                "EXIT_CLAIM", f"exit_mark:{reason}", round(ratio, 3),
                "CONSISTENT" if ok else "INCONSISTENT",
                f"exit/entry ratio {ratio:.2f} for {reason}", {}))
    return findings


# ----------------------------------------------------------------------
# nightly runner
# ----------------------------------------------------------------------

async def _persist(db: Database, decision_id: str,
                   findings: list[Finding], now: datetime) -> int:
    written = 0
    for f in findings:
        written += await db.execute(
            "INSERT OR IGNORE INTO rhetoric_audits(audit_id, decision_id, "
            "audited_at, audit_type, subject, claim_value, grade, detail, "
            "context_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (f"rha_{new_uuid()[:12]}", decision_id, iso_utc(now),
             f.audit_type, f.subject, f.claim_value, f.grade, f.detail,
             json.dumps(f.context, default=str)))
    return written


async def run_nightly(db: Database, scoring: dict[str, Any],
                      now: datetime | None = None) -> dict[str, Any]:
    """Audit today's decisions and today's closed trades. Idempotent."""
    cfg = scoring.get("rhetoric_audit", {})
    if not cfg.get("enabled", True):
        return {"enabled": False}
    tolerance = float(cfg.get("tolerance_rel", 0.0075))
    now = now or utc_now()
    day_start = to_et(now).replace(hour=0, minute=0, second=0, microsecond=0)
    since = day_start.astimezone(timezone.utc).isoformat(
        timespec="microseconds")

    summary = {"decisions": 0, "claims": 0, "supported": 0, "derived": 0,
               "unsupported": 0, "exits": 0, "consistent": 0,
               "inconsistent": 0, "not_checkable": 0}

    decisions = await db.fetchall(
        "SELECT decision_id, symbol FROM decisions WHERE created_at >= ? "
        "AND COALESCE(candidate_track,'') != 'CALIBRATION'", (since,))
    for decision in decisions:
        findings = await audit_decision_citations(db, decision, tolerance)
        if not findings:
            continue
        summary["decisions"] += 1
        await _persist(db, decision["decision_id"], findings, now)
        for f in findings:
            summary["claims"] += 1
            key = f.grade.lower()
            summary[key] = summary.get(key, 0) + 1

    trades = await db.fetchall(
        "SELECT t.*, d.symbol FROM trade_journal t "
        "JOIN decisions d ON d.decision_id = t.decision_id "
        "WHERE t.status='CLOSED' AND t.closed_at >= ?", (since,))
    for trade in trades:
        findings = await audit_closed_trade(db, trade, tolerance)
        if not findings:
            continue
        summary["exits"] += 1
        await _persist(db, trade["decision_id"], findings, now)
        for f in findings:
            key = f.grade.lower()
            summary[key] = summary.get(key, 0) + 1

    level = ("WARN" if summary["unsupported"] or summary["inconsistent"]
             else "INFO")
    await db.log_event(
        level, "rhetoric_audit", "RHETORIC_AUDIT",
        f"{summary['claims']} citation claim(s): "
        f"{summary['supported']} supported, {summary['derived']} derived, "
        f"{summary['unsupported']} UNSUPPORTED; "
        f"{summary['exits']} exit(s): {summary['consistent']} consistent, "
        f"{summary['inconsistent']} INCONSISTENT", summary)
    return summary
