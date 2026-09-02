"""
Alpha Council v2.5 §12 - deterministic promotion rules.

The recommendation is arithmetic over the two StrategyPerformance
records; no model can override a failed rule (§17.7), and during the
competition PROMOTE_CHALLENGER is advisory — operator approval is
mandatory and the registry's promote() enforces it independently.

The default competition posture is the one the spec calls the honest
one: CONTINUE_SHADOW, because the sample is tiny.

Place at: alpha_council/evolution/promotion.py
"""

from __future__ import annotations

import json
from typing import Any

from alpha_council.db.engine import Database
from alpha_council.models.evolution import (
    PromotionRecommendation,
    StrategyPerformance,
)
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.time import iso_utc, utc_now

# Above this share of unmeasured challenger observations, the comparison
# itself is not trustworthy enough to act on.
MAX_UNMEASURED_SHARE = 0.25


def recommend(champion: StrategyPerformance,
              challenger: StrategyPerformance,
              sessions_observed: int,
              evolution_cfg: dict[str, Any] | None = None
              ) -> PromotionRecommendation:
    cfg = (evolution_cfg or {}).get("competition", {})
    min_obs = int(cfg.get("min_observations_to_recommend_promotion", 12))
    min_sessions = int(cfg.get("min_sessions_to_recommend_promotion", 2))

    reasons: list[str] = []
    failed: list[str] = []

    # ---- evidence sufficiency ------------------------------------
    if challenger.observations < min_obs:
        reasons.append(
            f"{challenger.observations} shadow observations; "
            f"{min_obs} required before a promotion can be recommended")
    if sessions_observed < min_sessions:
        reasons.append(
            f"{sessions_observed} session(s) observed; {min_sessions} "
            "required")
    measured = challenger.observations - challenger.unmeasured_observations
    if challenger.observations and (challenger.unmeasured_observations
                                    / challenger.observations
                                    > MAX_UNMEASURED_SHARE):
        reasons.append(
            f"{challenger.unmeasured_observations}/{challenger.observations}"
            " observations unmeasurable; the comparison is not trustworthy")

    insufficient = bool(reasons)

    # ---- hard promotion rules (§12.2, competition-scaled) --------
    if not challenger.total_pnl > champion.total_pnl:
        failed.append("challenger total P&L does not exceed the champion's")
    if challenger.max_drawdown_pct > champion.max_drawdown_pct + 1e-9:
        failed.append("challenger max drawdown exceeds the champion's")
    if (challenger.expectancy is not None and champion.expectancy is not None
            and not challenger.expectancy > champion.expectancy):
        failed.append("challenger expectancy does not exceed the champion's")

    if insufficient:
        recommendation = "CONTINUE_SHADOW"
        strength = "INSUFFICIENT"
    elif failed:
        # Sufficient evidence, rules failed. Clearly worse -> keep;
        # ambiguous -> keep watching.
        materially_worse = challenger.total_pnl < champion.total_pnl
        recommendation = ("KEEP_CHAMPION" if materially_worse
                          else "CONTINUE_SHADOW")
        strength = "LOW" if measured < 25 else "MEDIUM"
    else:
        recommendation = "PROMOTE_CHALLENGER"
        strength = "LOW" if measured < 25 else "MEDIUM"
        reasons.append("all deterministic promotion rules passed; operator "
                       "approval still required in competition mode")

    return PromotionRecommendation(
        champion_id=champion.strategy_id,
        challenger_id=challenger.strategy_id,
        generated_at=utc_now(),
        champion_performance=champion,
        challenger_performance=challenger,
        recommendation=recommendation,
        evidence_strength=strength,
        reasons=reasons,
        failed_promotion_rules=failed,
        operator_approval_required=True,
    )


async def persist(db: Database, rec: PromotionRecommendation) -> None:
    await db.execute(
        "INSERT INTO promotion_recommendations(recommendation_id, "
        "champion_id, challenger_id, generated_at, recommendation, "
        "evidence_strength, reasons_json, failed_rules_json, "
        "operator_approval_required, approved_by_operator, approved_at) "
        "VALUES(?,?,?,?,?,?,?,?,1,NULL,NULL)",
        (f"prm_{new_uuid()[:12]}", rec.champion_id, rec.challenger_id,
         iso_utc(rec.generated_at), rec.recommendation,
         rec.evidence_strength, json.dumps(rec.reasons),
         json.dumps(rec.failed_promotion_rules)))
    await db.log_event(
        "INFO", "promotion", "PROMOTION_RECOMMENDED",
        f"{rec.challenger_id}: {rec.recommendation} "
        f"({rec.evidence_strength})",
        {"failed_rules": rec.failed_promotion_rules,
         "reasons": rec.reasons})
