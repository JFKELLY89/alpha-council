"""
Alpha Council v2.5 §8 - Pre-Market Strategist.

One Sol call around 08:45 ET turning overnight facts into a session
context brief. The brief is CONTEXT ONLY: it reaches the PM, Catalyst and
Red Team evidence packages as prose, and by construction it cannot touch
a score, a gate, a weight, or a risk limit.

Deterministic inputs, from the database and current snapshots:
  - overnight intelligence events (news + SEC), top by catalyst score;
  - SPY/QQQ/IWM/DIA gap/return context;
  - open positions and their exposure;
  - the prior sessions' strategy lessons;
  - the configured blackout windows;
  - the current champion configuration version.

Idempotent per session: the brief for a date is generated once and reused
on restart (premarket_briefs.session_date is UNIQUE). A failed generation
degrades to a normal scan with no brief (v2.5 §28).

Place at: alpha_council/agents/premarket.py
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Sequence

from alpha_council.agents.evidence import EvidencePackage, estimate_tokens
from alpha_council.agents.llm import LLMClient
from alpha_council.db.engine import Database
from alpha_council.models.evolution import PreMarketBrief
from alpha_council.utils.ids import input_hash, new_uuid
from alpha_council.utils.time import iso_utc, to_et, utc_now

BENCHMARKS = ("SPY", "QQQ", "IWM", "DIA")


class PreMarketStrategist:
    """Generates and persists the daily session brief."""

    def __init__(self, client: LLMClient, db: Database, market: Any,
                 config: dict[str, Any],
                 blackouts: Sequence[Any] = (),
                 champion_id: str = ""):
        self.client = client
        self.db = db
        self.market = market
        self.config = config
        self.blackouts = list(blackouts)
        self.champion_id = champion_id
        self._prompt: str | None = None

    def prompt(self) -> str:
        if self._prompt is None:
            from alpha_council.settings import load_prompt

            self._prompt = load_prompt("premarket_system")
        return self._prompt

    # ---- entry point ------------------------------------------------

    async def daily_brief(self, session_id: str = "premarket"
                          ) -> PreMarketBrief | None:
        """Today's brief: stored copy if it exists, else generate once."""
        session_date = str(to_et(utc_now()).date())
        stored = await self.db.fetchone(
            "SELECT output_json FROM premarket_briefs WHERE session_date=?",
            (session_date,))
        if stored:
            try:
                return PreMarketBrief.model_validate_json(
                    stored["output_json"])
            except Exception:  # noqa: BLE001 - regenerate over a bad row
                pass
        return await self.generate(session_date, session_id)

    async def generate(self, session_date: str,
                       session_id: str = "premarket"
                       ) -> PreMarketBrief | None:
        sections = await self._build_context(session_date)
        package = EvidencePackage(symbol="SESSION", as_of=utc_now(),
                                  role="PM", sections=sections)
        package.token_estimate = estimate_tokens(package.to_json())

        result = await self.client.call(
            "briefing", self.prompt(), package, PreMarketBrief,
            session_id=session_id, estimated_cost=0.05)

        if result.failed or not isinstance(result.parsed, PreMarketBrief):
            await self.db.log_event(
                "WARN", "premarket", "BRIEF_FAILED",
                result.error or "no valid brief; scanning without one")
            return None

        brief = result.parsed.model_copy(update={
            "session_date": session_date,     # ours, not the model's
            "generated_at": utc_now(),
        })
        await self.db.execute(
            "INSERT OR REPLACE INTO premarket_briefs(brief_id, session_date, "
            "generated_at, model, output_json, input_hash, cost_usd) "
            "VALUES(?,?,?,?,?,?,?)",
            (f"brf_{new_uuid()[:10]}", session_date,
             iso_utc(brief.generated_at), result.model,
             brief.model_dump_json(), input_hash(package.to_json()),
             result.cost_usd))
        await self.db.log_event(
            "INFO", "premarket", "BRIEF_GENERATED",
            f"{brief.session_bias}, confidence {brief.confidence:.2f}",
            {"session_date": session_date, "cost_usd": result.cost_usd})
        return brief

    # ---- deterministic context --------------------------------------

    async def _build_context(self, session_date: str) -> dict[str, Any]:
        now = utc_now()
        since = iso_utc(now - timedelta(hours=18))

        overnight = await self.db.fetchall(
            "SELECT symbol, event_type, direction, "
            "ROUND(catalyst_score,1) AS catalyst, "
            "ROUND(materiality_score,1) AS materiality, "
            "extracted_facts_json FROM intelligence_events "
            "WHERE created_at >= ? ORDER BY catalyst_score DESC LIMIT 15",
            (since,))
        for row in overnight:
            try:
                row["facts"] = json.loads(
                    row.pop("extracted_facts_json") or "[]")[:2]
            except (ValueError, TypeError):
                row["facts"] = []

        benchmarks: dict[str, Any] = {}
        try:
            snaps = await self.market.snapshots(list(BENCHMARKS))
            for sym, snap in snaps.items():
                price = snap.signal_price() or snap.mid
                prev = snap.prev_close
                benchmarks[sym] = {
                    "price": round(price, 2) if price else None,
                    "overnight_return_pct": (
                        round((price - prev) / prev * 100, 3)
                        if price and prev else None),
                }
        except Exception:  # noqa: BLE001 - the brief survives without them
            benchmarks = {"note": "benchmark snapshots unavailable"}

        positions = await self.db.fetchall(
            "SELECT d.symbol, t.qty, t.entry_debit, t.candidate_track "
            "FROM trade_journal t "
            "LEFT JOIN decisions d ON d.decision_id = t.decision_id "
            "WHERE t.status='OPEN'")

        lessons = await self.db.fetchall(
            "SELECT lesson_type, confidence, substr(observation,1,200) AS "
            "observation FROM strategy_lessons "
            "ORDER BY created_at DESC LIMIT 6")

        return {
            "instruction": (
                "Produce a PreMarketBrief for the trading session. Use only "
                "the facts below. The brief is context: it cannot change "
                "scores, gates, weights, or risk limits."),
            "session_date": session_date,
            "champion_strategy": self.champion_id or "unversioned",
            "overnight_events": overnight,
            "benchmark_context": benchmarks,
            "open_positions": positions,
            "known_risk_windows": [
                {"name": b.name, "at_et": str(b.timestamp_et),
                 "pre_minutes": b.pre_block_minutes,
                 "post_minutes": b.post_block_minutes}
                for b in self.blackouts[:8]],
            "prior_session_lessons": lessons,
        }
