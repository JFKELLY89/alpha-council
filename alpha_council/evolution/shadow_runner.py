"""
Alpha Council v2.5 §19 - challenger shadow evaluation.

Re-ranks each stored scan's candidates under the challenger's weights and
floors, and records what the challenger WOULD have done. Deterministic
throughout: the component scores were computed once by the live scanner
and are re-weighted here — no LLM re-runs, no market data, no invention.

What this can and cannot measure, stated plainly:

  MEASURABLE. Candidates the champion took to council: the challenger's
  would/would-not decision lands on a decision_id whose outcome (a real
  fill, a rejection, a marked rejected-shadow) the database knows.

  UNMEASURABLE. A candidate the challenger would promote that never
  reached a council has no priced structure and no marks. Those rows are
  recorded with would_trade=1 and counted as unmeasured in the
  performance comparison — never silently dropped, never invented.

During the competition the challenger is shadow-only by construction:
nothing in this module touches Alpaca, orders, or the live config.

Place at: alpha_council/evolution/shadow_runner.py
"""

from __future__ import annotations

import json
from typing import Any

from alpha_council.db.engine import Database
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.math import clip, weighted_sum
from alpha_council.utils.time import iso_utc, utc_now

# Defaults mirror quant/scoring.py so a challenger config that omits a
# weight set falls back to the same numbers the champion uses.
from alpha_council.quant.scoring import (
    DEFAULT_OPP_WEIGHTS_EVENT,
    DEFAULT_OPP_WEIGHTS_MOMENTUM,
    DEFAULT_PRE_WEIGHTS_EVENT,
    DEFAULT_PRE_WEIGHTS_MOMENTUM,
)


class ShadowRunner:
    def __init__(self, db: Database):
        self.db = db

    async def evaluate_recent_scans(self, strategy_id: str,
                                    challenger_config: dict[str, Any],
                                    lookback_hours: int = 30) -> int:
        """Shadow-evaluate every scan in the window not yet evaluated.

        Returns the number of shadow decisions written.
        """
        from datetime import timedelta

        since = iso_utc(utc_now() - timedelta(hours=lookback_hours))
        scans = await self.db.fetchall(
            "SELECT scan_id FROM scan_runs WHERE started_at >= ? "
            "AND status='COMPLETE' ORDER BY started_at", (since,))

        written = 0
        for scan in scans:
            # evaluate_scan is idempotent per (strategy, source): re-running
            # a window completes partial evaluations instead of skipping.
            written += await self.evaluate_scan(scan["scan_id"], strategy_id,
                                                challenger_config)
        return written

    async def evaluate_scan(self, scan_id: str, strategy_id: str,
                            challenger_config: dict[str, Any]) -> int:
        rows = await self.db.fetchall(
            "SELECT c.*, d.decision_id FROM candidate_scores c "
            "LEFT JOIN decisions d ON d.candidate_id = c.candidate_id "
            "WHERE c.scan_id=?", (scan_id,))
        if not rows:
            return 0

        rescored = [self._rescore(row, challenger_config) for row in rows]
        selected_ids = self._select(rescored, challenger_config)

        now = iso_utc()
        written = 0
        for entry in rescored:
            source = (entry["decision_id"]
                      or f"cand:{entry['candidate_id']}")
            exists = await self.db.fetchone(
                "SELECT 1 FROM strategy_shadow_decisions "
                "WHERE strategy_id=? AND source_decision_id=?",
                (strategy_id, source))
            if exists:
                continue
            would_trade = entry["candidate_id"] in selected_ids
            structure_id = None
            if would_trade:
                srow = await self.db.fetchone(
                    "SELECT structure_id FROM option_structures "
                    "WHERE candidate_id=? ORDER BY rank LIMIT 1",
                    (entry["candidate_id"],))
                structure_id = (srow or {}).get("structure_id")
            await self.db.execute(
                "INSERT INTO strategy_shadow_decisions(shadow_decision_id, "
                "source_decision_id, strategy_id, evaluated_at, would_trade, "
                "selected_structure_id, requested_risk_pct, "
                "hypothetical_qty, rationale_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (f"ssd_{new_uuid()[:12]}", source, strategy_id, now,
                 1 if would_trade else 0, structure_id, None, None,
                 json.dumps({
                     "scan_id": scan_id,
                     "symbol": entry["symbol"],
                     "track": entry["track"],
                     "challenger_final": round(entry["final"], 2),
                     "champion_final": round(entry["champion_final"], 2),
                     "floor": entry["floor"],
                     "measurable": entry["decision_id"] is not None,
                 })))
            written += 1
        return written

    # ---- deterministic re-scoring -----------------------------------

    def _rescore(self, row: dict[str, Any],
                 config: dict[str, Any]) -> dict[str, Any]:
        track = (row.get("candidate_track") or "MOMENTUM").upper()
        is_event = track == "EVENT"

        components = {
            "momentum": row["momentum_score"],
            "relative_volume": row["relative_volume_score"],
            "trend_regime": row["trend_regime_score"],
            "relative_strength": row["relative_strength_score"],
            "options_opportunity": row["options_opportunity_score"],
            "options_liquidity": row["options_liquidity_score"],
        }
        if is_event:
            components.update({
                "catalyst": row["catalyst_score"],
                "corroboration": row["corroboration_score"],
                "novelty": row["novelty_score"],
            })
            opp_w = config.get("opportunity_weights_event",
                               DEFAULT_OPP_WEIGHTS_EVENT)
            pre_w = config.get("pre_score_weights_event",
                               DEFAULT_PRE_WEIGHTS_EVENT)
        else:
            opp_w = config.get("opportunity_weights_momentum",
                               DEFAULT_OPP_WEIGHTS_MOMENTUM)
            pre_w = config.get("pre_score_weights_momentum",
                               DEFAULT_PRE_WEIGHTS_MOMENTUM)

        raw = weighted_sum(components, opp_w)
        final = clip(raw * row["data_confidence_factor"]
                     * row["regime_factor"] * row["event_risk_factor"])
        pre = weighted_sum({k: v for k, v in components.items()
                            if k in pre_w}, pre_w)

        tier1 = config.get("tiers", {}).get(1, {})
        base_floor = float(tier1.get("final_score_floor", 62.0))
        return {
            "candidate_id": row["candidate_id"],
            "decision_id": row.get("decision_id"),
            "symbol": row["symbol"],
            "track": track,
            "pre": pre,
            "final": final,
            "champion_final": row["final_opportunity_score"],
            # Mirrors rank_by_track: EVENT prices against its own bar.
            "floor": (float(tier1.get("final_score_floor_event", base_floor))
                      if str(track) == "EVENT" else base_floor),
            "pre_floor": float(tier1.get("pre_score_floor", 58.0)),
        }

    @staticmethod
    def _select(rescored: list[dict[str, Any]],
                config: dict[str, Any]) -> set[str]:
        """Track-quota selection under the challenger's floors.

        Mirrors quant/scoring.rank_by_track at tier-1 semantics: shadowing
        replays the day's scans as if the challenger opened the session.
        """
        quota = (config.get("tracks", {}) or {}).get(
            "final_quota", {"EVENT": 3, "MOMENTUM": 2})
        total = int((config.get("discovery", {}) or {}).get(
            "final_candidate_top_n", 5))
        per_scan = int((config.get("tiers", {}) or {}).get(1, {}).get(
            "max_councils_per_scan", 3))

        eligible = [e for e in rescored
                    if e["final"] >= e["floor"] and e["pre"] >= e["pre_floor"]]
        by_track: dict[str, list[dict[str, Any]]] = {"EVENT": [],
                                                     "MOMENTUM": []}
        for e in eligible:
            by_track.setdefault(e["track"], []).append(e)
        for entries in by_track.values():
            entries.sort(key=lambda e: e["final"], reverse=True)

        chosen: list[dict[str, Any]] = []
        chosen += by_track.get("EVENT", [])[: int(quota.get("EVENT", 3))]
        chosen += by_track.get("MOMENTUM", [])[: int(quota.get("MOMENTUM", 2))]
        if len(chosen) < total:
            spare = [e for entries in by_track.values() for e in entries
                     if e not in chosen]
            spare.sort(key=lambda e: e["final"], reverse=True)
            chosen += spare[: total - len(chosen)]
        chosen.sort(key=lambda e: e["final"], reverse=True)
        return {e["candidate_id"] for e in chosen[:per_scan]}
