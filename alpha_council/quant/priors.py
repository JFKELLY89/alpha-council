"""
Alpha Council v2.6 - market-implied priors (hackathon review idea #5).

Autobelay (1st place) fed its model two independent "crowds" - Kalshi
range markets and the option chain's own implied distribution - then
Brier-scored both nightly against what the model had actually been
handed. This module implements the chain crowd for a small ETF cohort:

  EXPECTED MOVE. ATM straddle mid / spot at the first expiry on or past
  the horizon: the standard trader approximation of the one-sigma move.

  P(UP). The risk-neutral probability of finishing above spot, read from
  the tightest verticals that bracket spot:
      P(S > K) ~= (C(K_lo) - C(K_hi)) / (K_hi - K_lo)
  computed from calls and puts independently and averaged when both are
  usable. Risk-neutral, not real-world: for index ETFs p_up hovers near
  0.5 and that IS the honest prior.

The value is threefold: expected-move realism for PM targets and
invalidation levels; a non-LLM second opinion in the evidence package;
and a calibration yardstick - every usable prior is Brier-scored at
expiry against the realized close, so the cohort review can answer "was
the chain's crowd worth listening to?" with a measured number (0.25 is
the score of always saying 50/50).

No LLM calls, no new credentials, no new external dependency: everything
reads the Alpaca endpoints the options engine already uses. A second
source (Kalshi was Autobelay's) can be added later as another `source`
value without schema changes.

Place at: alpha_council/quant/priors.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from alpha_council.alpaca.market_data import MarketDataService
from alpha_council.alpaca.rest_client import AlpacaRestClient
from alpha_council.db.engine import Database
from alpha_council.options_engine.chain import parse_occ_symbol
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.math import safe_mid, spread_pct
from alpha_council.utils.time import ET, iso_utc, to_et, utc_now

SOURCE_CHAIN = "CHAIN"

# Idempotent DDL, applied at startup so a live database predating this
# feature gains the tables without a migration script. schema.sql carries
# the same definitions for fresh init_db runs.
PRIORS_DDL = (
    """
    CREATE TABLE IF NOT EXISTS market_priors (
        prior_id          TEXT PRIMARY KEY,
        symbol            TEXT NOT NULL,
        asof              TEXT NOT NULL,
        source            TEXT NOT NULL DEFAULT 'CHAIN',
        spot              REAL NOT NULL,
        expiry            TEXT NOT NULL,
        horizon_days      INTEGER NOT NULL,
        expected_move_pct REAL,
        p_up              REAL,
        usable            INTEGER NOT NULL CHECK(usable IN (0,1)),
        unusable_reason   TEXT,
        inputs_json       TEXT NOT NULL DEFAULT '{}',
        UNIQUE(symbol, source, asof)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_market_priors_symbol
        ON market_priors(symbol, asof DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS prior_scores (
        score_id             TEXT PRIMARY KEY,
        prior_id             TEXT NOT NULL UNIQUE REFERENCES market_priors(prior_id),
        scored_at            TEXT NOT NULL,
        realized_close       REAL NOT NULL,
        realized_return      REAL NOT NULL,
        outcome_up           INTEGER NOT NULL CHECK(outcome_up IN (0,1)),
        brier                REAL NOT NULL,
        within_expected_move INTEGER CHECK(within_expected_move IN (0,1))
    )
    """,
)

EVIDENCE_NOTE = (
    "The option market's own implied distribution for this underlying by "
    "the stated expiry: a one-sigma expected move approximated from the "
    "ATM straddle, and the risk-neutral probability of finishing above "
    "spot read from the verticals bracketing spot. A crowd estimate, not "
    "a signal - p near 0.5 is normal. Judge price targets and invalidation "
    "levels against the expected move: a thesis that needs a move far "
    "beyond it inside the horizon is mispriced hope, and one well inside "
    "it should say what the market's own distribution is missing."
)


# ----------------------------------------------------------------------
# pure math - testable without a broker
# ----------------------------------------------------------------------

@dataclass(slots=True)
class ChainImplied:
    expected_move_pct: float | None = None
    p_up: float | None = None
    p_up_calls: float | None = None
    p_up_puts: float | None = None
    atm_strike: float | None = None
    straddle_mid: float | None = None
    strike_gap: float | None = None
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.expected_move_pct is not None and self.p_up is not None


def _digital(lo_mid: float, hi_mid: float, lo: float, hi: float) -> float | None:
    """P(S > midpoint of [lo, hi]) from one vertical; None when degenerate."""
    width = hi - lo
    if width <= 0:
        return None
    p = (lo_mid - hi_mid) / width
    return min(1.0, max(0.0, p))


def chain_implied(spot: float, call_mids: dict[float, float],
                  put_mids: dict[float, float],
                  max_strike_gap_pct: float = 0.02) -> ChainImplied:
    """Expected move and p_up from usable mids keyed by strike.

    Callers pass only mids that already survived the per-leg usability
    gate. The bracketing pair straddles spot, so the digital approximates
    P(S > spot) with a bias no larger than half the strike gap - which is
    why a grid coarser than max_strike_gap_pct is refused as unreadable.
    """
    out = ChainImplied()
    if spot <= 0:
        out.reason = "no spot price"
        return out

    # ATM straddle: nearest strike quoted on BOTH sides.
    both = sorted(set(call_mids) & set(put_mids),
                  key=lambda k: (abs(k - spot), k))
    if both:
        k0 = both[0]
        out.atm_strike = k0
        out.straddle_mid = call_mids[k0] + put_mids[k0]
        out.expected_move_pct = out.straddle_mid / spot

    # Digital read from the tightest verticals bracketing spot.
    estimates: list[float] = []
    for mids, is_call in ((call_mids, True), (put_mids, False)):
        below = [k for k in mids if k <= spot]
        above = [k for k in mids if k > spot]
        if not below or not above:
            continue
        lo, hi = max(below), min(above)
        gap_pct = (hi - lo) / spot
        if gap_pct > max_strike_gap_pct:
            continue
        out.strike_gap = hi - lo
        if is_call:
            p = _digital(mids[lo], mids[hi], lo, hi)
            if p is not None:
                out.p_up_calls = round(p, 4)
                estimates.append(p)
        else:
            # Put mids rise with strike; the same vertical reads P(S < K).
            p_down = _digital(mids[hi], mids[lo], lo, hi)
            if p_down is not None:
                out.p_up_puts = round(1.0 - p_down, 4)
                estimates.append(1.0 - p_down)

    if estimates:
        out.p_up = round(sum(estimates) / len(estimates), 4)

    if out.expected_move_pct is None:
        out.reason = "no two-sided ATM quote for a straddle"
    elif out.p_up is None:
        out.reason = (f"no readable vertical within "
                      f"{max_strike_gap_pct:.0%} of spot")
    return out


# ----------------------------------------------------------------------
# service
# ----------------------------------------------------------------------

class PriorsService:
    """Computes, persists, serves and scores chain-implied priors."""

    def __init__(self, api: AlpacaRestClient, market: MarketDataService,
                 db: Database, scoring: dict[str, Any]):
        cfg = scoring.get("priors", {})
        self.api = api
        self.market = market
        self.db = db
        self.enabled = bool(cfg.get("enabled", True))
        self.cohort = [str(s).upper() for s in cfg.get("etf_cohort", [])]
        self.horizon_days = int(cfg.get("horizon_days", 7))
        self.refresh_minutes = float(cfg.get("refresh_minutes", 90))
        self.strike_band_pct = float(cfg.get("strike_band_pct", 0.08))
        self.max_leg_spread_pct = float(cfg.get("max_leg_spread_pct", 0.25))
        self.max_strike_gap_pct = float(cfg.get("max_strike_gap_pct", 0.02))
        self.review_date = str(cfg.get("cohort_review_date") or "")
        self.scoring = scoring

    async def ensure_schema(self) -> None:
        for ddl in PRIORS_DDL:
            await self.db.execute(ddl)

    # ---- computation --------------------------------------------------

    async def compute_cohort(self, now: datetime | None = None
                             ) -> dict[str, dict[str, Any]]:
        """Fresh priors for every cohort symbol; returns evidence dicts.

        Per-symbol refresh gate: a prior younger than refresh_minutes is
        reused, so nine scans a day cost roughly four computes per symbol.
        Every failure is per-symbol and logged; one bad chain never costs
        the rest of the cohort.
        """
        if not (self.enabled and self.cohort):
            return {}
        now = now or utc_now()
        out: dict[str, dict[str, Any]] = {}

        stale = [s for s in self.cohort]
        fresh_cutoff = iso_utc(now - timedelta(minutes=self.refresh_minutes))
        for symbol in list(stale):
            row = await self._latest_row(symbol)
            if row and row["asof"] >= fresh_cutoff:
                stale.remove(symbol)
                if row["usable"]:
                    out[symbol] = self._evidence(row)

        if not stale:
            return out
        try:
            snaps = await self.market.snapshots(stale)
        except Exception as exc:  # noqa: BLE001 - priors never cost a scan
            await self.db.log_event("WARN", "priors", "PRIOR_SNAPSHOTS_FAILED",
                                    str(exc)[:200])
            return out

        for symbol in stale:
            snap = snaps.get(symbol)
            spot = (snap.quote.signal_price() or snap.mid) if snap else None
            if not spot or spot <= 0:
                continue
            try:
                row = await self.compute_one(symbol, float(spot), now)
            except Exception as exc:  # noqa: BLE001
                await self.db.log_event(
                    "WARN", "priors", "PRIOR_COMPUTE_FAILED",
                    f"{symbol}: {type(exc).__name__}: {exc}"[:220])
                continue
            if row and row["usable"]:
                out[symbol] = self._evidence(row)
        return out

    async def compute_one(self, symbol: str, spot: float,
                          now: datetime | None = None) -> dict[str, Any]:
        """One chain-implied prior, persisted usable or not (audit trail)."""
        now = now or utc_now()
        today = to_et(now).date()
        target = today + timedelta(days=self.horizon_days)

        expiry, occs = await self._target_expiry(symbol, spot, target)
        call_mids: dict[float, float] = {}
        put_mids: dict[float, float] = {}
        legs_seen = 0
        if occs:
            snaps = await self.api.get_option_snapshots(occs)
            for occ, snap in snaps.items():
                parsed = parse_occ_symbol(occ)
                if parsed is None:
                    continue
                _, _, opt_type, strike = parsed
                quote = (snap or {}).get("latestQuote") or {}
                bid, ask = quote.get("bp"), quote.get("ap")
                mid = safe_mid(bid, ask)
                if mid is None:
                    continue
                legs_seen += 1
                sp = spread_pct(bid, ask) or 1.0
                # Percentage spreads overstate cheap wings; a dime-wide
                # quote is readable whatever the percentage says.
                if sp > self.max_leg_spread_pct and (float(ask) - float(bid)) > 0.10:
                    continue
                (call_mids if opt_type == "CALL" else put_mids)[strike] = mid

        implied = chain_implied(spot, call_mids, put_mids,
                                self.max_strike_gap_pct)
        reason = implied.reason
        if not occs:
            reason = "no contracts in the expiry window"

        row = {
            "prior_id": f"pri_{new_uuid()[:12]}",
            "symbol": symbol,
            "asof": iso_utc(now),
            "source": SOURCE_CHAIN,
            "spot": spot,
            "expiry": expiry.isoformat() if expiry else target.isoformat(),
            "horizon_days": self.horizon_days,
            "expected_move_pct": implied.expected_move_pct,
            "p_up": implied.p_up,
            "usable": 1 if implied.usable else 0,
            "unusable_reason": None if implied.usable else reason,
            "inputs_json": json.dumps({
                "atm_strike": implied.atm_strike,
                "straddle_mid": implied.straddle_mid,
                "p_up_calls": implied.p_up_calls,
                "p_up_puts": implied.p_up_puts,
                "strike_gap": implied.strike_gap,
                "usable_calls": len(call_mids),
                "usable_puts": len(put_mids),
                "legs_quoted": legs_seen,
            }),
        }
        await self.db.execute(
            "INSERT OR IGNORE INTO market_priors(prior_id, symbol, asof, "
            "source, spot, expiry, horizon_days, expected_move_pct, p_up, "
            "usable, unusable_reason, inputs_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["prior_id"], row["symbol"], row["asof"], row["source"],
             row["spot"], row["expiry"], row["horizon_days"],
             row["expected_move_pct"], row["p_up"], row["usable"],
             row["unusable_reason"], row["inputs_json"]))
        return row

    async def _target_expiry(self, symbol: str, spot: float, target: date
                             ) -> tuple[date | None, list[str]]:
        """First listed expiry on/after the horizon, with nearby strikes.

        Two-step fetch keeps payloads small for daily-expiry names while
        still finding a monthly-only chain (SCHD): a tight window first,
        then a 35-day reach only if the tight window is empty.
        """
        for reach in (7, 35):
            contracts = await self.api.get_option_contracts(
                symbol,
                expiration_gte=target.isoformat(),
                expiration_lte=(target + timedelta(days=reach)).isoformat(),
                strike_gte=spot * (1 - self.strike_band_pct),
                strike_lte=spot * (1 + self.strike_band_pct),
                limit=500)
            by_exp: dict[str, list[str]] = {}
            for c in contracts:
                exp = str(c.get("expiration_date") or "")
                occ = str(c.get("symbol") or "")
                if exp and occ:
                    by_exp.setdefault(exp, []).append(occ)
            if by_exp:
                first = min(by_exp)
                return date.fromisoformat(first), by_exp[first]
        return None, []

    # ---- serving ------------------------------------------------------

    async def _latest_row(self, symbol: str) -> dict[str, Any] | None:
        return await self.db.fetchone(
            "SELECT * FROM market_priors WHERE symbol=? AND source=? "
            "ORDER BY asof DESC LIMIT 1", (symbol, SOURCE_CHAIN))

    @staticmethod
    def _evidence(row: dict[str, Any]) -> dict[str, Any]:
        em = row.get("expected_move_pct")
        return {
            "source": "option chain (risk-neutral), Alpaca Indicative feed",
            "asof": row["asof"],
            "expiry": row["expiry"],
            "spot_at_calc": row["spot"],
            "expected_move_pct": round(float(em) * 100, 2) if em else None,
            "p_finish_above_spot": (round(float(row["p_up"]), 3)
                                    if row.get("p_up") is not None else None),
            "note": EVIDENCE_NOTE,
        }

    # ---- Brier scoring ------------------------------------------------

    async def score_due(self, now: datetime | None = None) -> int:
        """Score every usable, unscored prior whose expiry has passed.

        Scored once, at the expiry's daily close, against the spot the
        prior was computed from - exactly what the model was handed, which
        is the Autobelay discipline this idea was imported for.
        """
        now = now or utc_now()
        today = to_et(now).date()
        rows = await self.db.fetchall(
            "SELECT p.* FROM market_priors p "
            "LEFT JOIN prior_scores s ON s.prior_id = p.prior_id "
            "WHERE s.score_id IS NULL AND p.usable = 1 AND p.expiry < ? "
            "ORDER BY p.expiry LIMIT 200", (today.isoformat(),))
        if not rows:
            return 0

        closes: dict[tuple[str, str], float] = {}
        for expiry in sorted({r["expiry"] for r in rows}):
            symbols = sorted({r["symbol"] for r in rows
                              if r["expiry"] == expiry})
            day = date.fromisoformat(expiry)
            start = datetime.combine(day, datetime.min.time(), tzinfo=ET)
            try:
                bars = await self.api.get_stock_bars(
                    symbols, "1Day", start, start + timedelta(days=1))
            except Exception as exc:  # noqa: BLE001
                await self.db.log_event(
                    "WARN", "priors", "PRIOR_SCORE_BARS_FAILED",
                    f"{expiry}: {exc}"[:200])
                continue
            for symbol, symbol_bars in (bars or {}).items():
                if symbol_bars:
                    closes[(symbol, expiry)] = float(symbol_bars[-1]["c"])

        scored = 0
        for r in rows:
            close = closes.get((r["symbol"], r["expiry"]))
            if close is None or not r["spot"]:
                continue
            realized = close / float(r["spot"]) - 1.0
            outcome_up = 1 if close > float(r["spot"]) else 0
            brier = (float(r["p_up"]) - outcome_up) ** 2
            em = r.get("expected_move_pct")
            within = (1 if em is not None and abs(realized) <= float(em)
                      else 0 if em is not None else None)
            await self.db.execute(
                "INSERT OR IGNORE INTO prior_scores(score_id, prior_id, "
                "scored_at, realized_close, realized_return, outcome_up, "
                "brier, within_expected_move) VALUES(?,?,?,?,?,?,?,?)",
                (f"psc_{new_uuid()[:12]}", r["prior_id"], iso_utc(now),
                 close, round(realized, 6), outcome_up, round(brier, 6),
                 within))
            scored += 1
        return scored

    async def brier_summary(self) -> list[dict[str, Any]]:
        """Per-symbol calibration record. 0.25 = the always-50/50 score."""
        return await self.db.fetchall(
            "SELECT p.symbol, COUNT(*) AS scored, "
            "ROUND(AVG(s.brier), 4) AS mean_brier, "
            "ROUND(AVG(s.within_expected_move) * 100, 1) AS within_em_pct, "
            "ROUND(AVG(ABS(p.p_up - 0.5)), 3) AS mean_tilt, "
            "ROUND(AVG(ABS(s.realized_return)) * 100, 2) AS mean_abs_move_pct "
            "FROM prior_scores s "
            "JOIN market_priors p ON p.prior_id = s.prior_id "
            "GROUP BY p.symbol ORDER BY p.symbol")

    # ---- the three-week cohort review ----------------------------------

    async def maybe_cohort_review(self, now: datetime | None = None
                                  ) -> str | None:
        """Write the one-shot ETF cohort review on/after the review date.

        Run-once semantics survive restarts via system_state, and a desk
        that was down on the date itself writes the review on its next
        post-close instead of silently skipping the commitment.
        """
        if not self.review_date:
            return None
        now = now or utc_now()
        if to_et(now).date() < date.fromisoformat(self.review_date):
            return None
        state_key = f"etf_cohort_review_done_{self.review_date}"
        if await self.db.get_state(state_key):
            return None

        from alpha_council.evolution.cohort_review import write_cohort_review

        path = await write_cohort_review(self.db, self.scoring, now=now)
        await self.db.set_state(state_key, iso_utc(now))
        return str(path)
