"""
Alpha Council v2.5 §10 - the Alpha Evolution agent.

One post-close call that reads accumulated lessons and deterministic
performance statistics and proposes AT MOST one bounded challenger — or,
far more often on a competition sample, NO CHANGE with a stated reason.

The guardrails are code, not prompt hopes:

  DETERMINISTIC PRE-GATE. Below min_observations_to_propose the LLM is
  not even called. A model cannot be tempted by evidence it never sees.

  THE VALIDATOR HAS THE LAST WORD. Every proposal passes
  change_validator before storage; a single immutable-path touch or
  out-of-bounds move rejects the whole proposal, and the rejection is
  itself persisted for the audit trail.

  IDENTITY IS OURS. challenger_id, parent id and created_at are assigned
  by the registry, whatever the model wrote in those fields.

Place at: alpha_council/agents/alpha_evolution.py
"""

from __future__ import annotations

from typing import Any

from alpha_council.agents.evidence import EvidencePackage, estimate_tokens
from alpha_council.agents.llm import LLMClient
from alpha_council.db.engine import Database
from alpha_council.evolution.champion import ChampionRegistry, PromotionRefused
from alpha_council.evolution.change_validator import (
    DEFAULT_MUTABLE_PREFIXES,
    apply_changes,
    resolve_path,
    validate_changes,
)
from alpha_council.models.evolution import ChallengerProposal, EvolutionDecision
from alpha_council.utils.time import utc_now

# The mutable surface shown to the model: current values for the paths it
# is allowed to touch, so its champion_value claims can be exact.
SNAPSHOT_PATHS = (
    "tiers.1.pre_score_floor",
    "tiers.1.final_score_floor",
    "tiers.1.pm_confidence_floor",
    "tiers.1.max_councils_per_scan",
    "tiers.1.max_councils_per_day",
    "tracks.final_quota.EVENT",
    "tracks.final_quota.MOMENTUM",
    "discovery.stage0_top_n",
    "discovery.options_prescreen_top_n",
    "discovery.final_candidate_top_n",
    "pre_score_weights_momentum.momentum",
    "pre_score_weights_momentum.relative_volume",
    "opportunity_weights_momentum.momentum",
    "opportunity_weights_momentum.relative_volume",
    "opportunity_weights_event.catalyst",
    "opportunity_weights_event.momentum",
    "structure_weights.liquidity",
    "structure_weights.reward_risk",
)


class AlphaEvolutionAgent:
    def __init__(self, client: LLMClient, db: Database,
                 registry: ChampionRegistry, config: dict[str, Any]):
        self.client = client
        self.db = db
        self.registry = registry
        self.config = config
        self.evolution_cfg = config.get("alpha_evolution", {})
        self._prompt: str | None = None

    def prompt(self) -> str:
        if self._prompt is None:
            from alpha_council.settings import load_prompt

            self._prompt = load_prompt("alpha_evolution_system")
        return self._prompt

    async def post_close_review(self, brief_sections: dict[str, Any],
                                session_id: str = "evolution"
                                ) -> ChallengerProposal | None:
        """The 16:30 review. Returns a STORED challenger, or None."""
        champion = await self.registry.current_champion()
        if champion is None:
            await self.db.log_event("WARN", "evolution", "NO_CHAMPION",
                                    "no champion version; skipping review")
            return None

        # One hypothesis at a time (§11.3).
        if await self.registry.active_challenger() is not None:
            await self.db.log_event(
                "INFO", "evolution", "CHALLENGER_ALREADY_ACTIVE",
                "existing challenger continues shadowing; no new proposal")
            return None

        # Deterministic evidence pre-gate: below the floor, NO CHANGE
        # without spending a token.
        min_obs = int(self.evolution_cfg.get("competition", {}).get(
            "min_observations_to_propose", 8))
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM decisions")
        observations = int((row or {}).get("n") or 0)
        if observations < min_obs:
            await self.db.log_event(
                "INFO", "evolution", "NO_CHANGE",
                f"{observations} observations < {min_obs} floor; the "
                "correct evolution output is NO CHANGE")
            return None

        champion_config = await self.registry.champion_config()
        sections = dict(brief_sections)
        sections["champion"] = {
            "strategy_id": champion["strategy_id"],
            "mutable_parameters": {
                path: resolve_path(champion_config, path)
                for path in SNAPSHOT_PATHS
                if resolve_path(champion_config, path) is not None},
            "mutable_prefixes": list(DEFAULT_MUTABLE_PREFIXES),
            "bounds": {
                "max_changes": int(self.evolution_cfg.get(
                    "max_changes_per_challenger", 3)),
                "max_move_pct_per_parameter": float(
                    self.evolution_cfg.get("hysteresis", {}).get(
                        "max_threshold_change_per_version_pct", 10.0)),
            },
        }

        package = EvidencePackage(symbol="STRATEGY", as_of=utc_now(),
                                  role="PM", sections=sections)
        package.token_estimate = estimate_tokens(package.to_json())

        result = await self.client.call(
            "alpha_evolution", self.prompt(), package, EvolutionDecision,
            session_id=session_id, estimated_cost=0.06)
        if result.failed or not isinstance(result.parsed, EvolutionDecision):
            await self.db.log_event(
                "WARN", "evolution", "EVOLUTION_CALL_FAILED",
                result.error or "no valid decision; champion unchanged")
            return None

        decision = result.parsed
        if not decision.propose or decision.proposal is None:
            await self.db.log_event(
                "INFO", "evolution", "NO_CHANGE",
                (decision.no_change_reason or "model declined")[:300])
            return None

        # Identity is ours, never the model's.
        proposal = decision.proposal.model_copy(update={
            "challenger_id": await self.registry.next_challenger_id(),
            "parent_champion_id": champion["strategy_id"],
            "created_at": utc_now(),
        })

        problems = validate_changes(proposal, champion_config,
                                    self.evolution_cfg)
        if problems:
            await self.registry.record_rejected_proposal(proposal, problems)
            await self.db.log_event(
                "WARN", "evolution", "CHALLENGER_REJECTED",
                f"{len(problems)} validator problem(s): {problems[0]}",
                {"problems": problems})
            return None

        challenger_config = apply_changes(champion_config, proposal.changes)
        try:
            await self.registry.store_challenger(proposal, challenger_config)
        except PromotionRefused as exc:
            await self.db.log_event("WARN", "evolution",
                                    "CHALLENGER_NOT_STORED", str(exc)[:200])
            return None
        return proposal
