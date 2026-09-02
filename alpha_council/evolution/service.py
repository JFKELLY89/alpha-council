"""
Alpha Council v2.5 §21 - the post-close evolution cycle.

    16:15  deterministic metrics            (scheduler post_close)
    16:20  post-trade lessons               (LessonGenerator)
    16:30  evolution review                 (AlphaEvolutionAgent)
    16:35  challenger shadow + performance  (ShadowRunner, performance)
           promotion recommendation         (promotion.recommend)

One coordinator so the scheduler calls one guarded method. Every step is
individually fenced: a lessons failure never blocks the shadow runner, an
evolution failure never blocks the performance snapshot, and nothing in
this file can reach an order, the live config, or a risk limit. Alpha
Evolution is non-load-bearing by construction (v2.5 §28).

Place at: alpha_council/evolution/service.py
"""

from __future__ import annotations

import json
from typing import Any

from alpha_council.agents.alpha_evolution import AlphaEvolutionAgent
from alpha_council.db.engine import Database
from alpha_council.evolution import performance as perf
from alpha_council.evolution import promotion as promo
from alpha_council.evolution.champion import ChampionRegistry
from alpha_council.evolution.lessons import LessonGenerator, build_brief
from alpha_council.evolution.shadow_runner import ShadowRunner


class EvolutionService:
    def __init__(self, db: Database, registry: ChampionRegistry,
                 lessons: LessonGenerator | None,
                 agent: AlphaEvolutionAgent | None,
                 shadow: ShadowRunner, config: dict[str, Any]):
        self.db = db
        self.registry = registry
        self.lessons = lessons
        self.agent = agent
        self.shadow = shadow
        self.config = config

    async def post_close_cycle(self) -> dict[str, Any]:
        """The whole 16:15-16:35 chain. Returns a summary for the log."""
        summary: dict[str, Any] = {}
        brief = None

        # 1. deterministic facts + lessons ---------------------------
        try:
            brief = await build_brief(self.db, lookback_days=7)
            summary["decisions_reviewed"] = brief.decision_count
            summary["closed_trades"] = brief.closed_trades
        except Exception as exc:  # noqa: BLE001
            await self._log("WARN", "EVOLUTION_BRIEF_FAILED", str(exc)[:200])

        if self.lessons is not None and brief is not None:
            try:
                lesson_set = await self.lessons.generate(brief)
                summary["lessons"] = (len(lesson_set.lessons)
                                      if lesson_set else 0)
            except Exception as exc:  # noqa: BLE001
                await self._log("WARN", "EVOLUTION_LESSONS_FAILED",
                                str(exc)[:200])

        # 2. at most one challenger proposal -------------------------
        if self.agent is not None and brief is not None:
            try:
                proposal = await self.agent.post_close_review(
                    brief.to_sections())
                summary["challenger_proposed"] = (proposal.challenger_id
                                                  if proposal else None)
            except Exception as exc:  # noqa: BLE001
                await self._log("WARN", "EVOLUTION_REVIEW_FAILED",
                                str(exc)[:200])

        # 3. shadow-evaluate + score the active challenger -----------
        challenger = await self.registry.active_challenger()
        if challenger is not None:
            try:
                cfg = json.loads(challenger["config_json"])
            except (ValueError, TypeError):
                cfg = {}
            try:
                written = await self.shadow.evaluate_recent_scans(
                    challenger["strategy_id"], cfg)
                summary["shadow_decisions"] = written
            except Exception as exc:  # noqa: BLE001
                await self._log("WARN", "EVOLUTION_SHADOW_FAILED",
                                str(exc)[:200])

            try:
                champion = await self.registry.current_champion()
                champ_perf = await perf.champion_performance(
                    self.db, champion["strategy_id"] if champion
                    else "alpha_v2_5_c0")
                chall_perf = await perf.challenger_performance(
                    self.db, challenger["strategy_id"])
                await perf.snapshot(self.db, champ_perf)
                await perf.snapshot(self.db, chall_perf)

                sessions = await self.db.fetchone(
                    "SELECT COUNT(DISTINCT substr(evaluated_at,1,10)) AS n "
                    "FROM strategy_shadow_decisions WHERE strategy_id=?",
                    (challenger["strategy_id"],))
                rec = promo.recommend(
                    champ_perf, chall_perf,
                    sessions_observed=int((sessions or {}).get("n") or 0),
                    evolution_cfg=self.config.get("alpha_evolution", {}))
                await promo.persist(self.db, rec)
                summary["promotion"] = rec.recommendation
            except Exception as exc:  # noqa: BLE001
                await self._log("WARN", "EVOLUTION_SCORING_FAILED",
                                str(exc)[:200])

        await self._log("INFO", "EVOLUTION_CYCLE_COMPLETE",
                        json.dumps(summary, default=str)[:400])
        return summary

    async def _log(self, level: str, event: str, message: str) -> None:
        try:
            await self.db.log_event(level, "evolution", event, message)
        except Exception:  # noqa: BLE001
            pass
