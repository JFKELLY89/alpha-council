"""
Alpha Council v2.5 §11 - Champion / Challenger registry.

The Champion is the live configuration; it alone creates paper orders.
A Challenger is a stored, validated hypothesis that shadows the same
opportunity stream. This module owns the strategy_versions ledger and
enforces the three structural rules:

  EXACTLY ONE CHAMPION. ensure_champion creates the first version from
  the live config; every decision stamps its strategy_id.

  ONE ACTIVE CHALLENGER (default). A second proposal while one is
  shadowing is refused — evidence accumulates on one hypothesis at a
  time (§11.3).

  VERSIONS ARE IMMUTABLE, PROMOTION IS EXPLICIT. A stored version's
  config_json never mutates; promotion during the competition requires
  operator_approved=True and is expected to come from a human at a
  keyboard, not from any scheduled path. Nothing in the scheduler calls
  promote().

Place at: alpha_council/evolution/champion.py
"""

from __future__ import annotations

import json
from typing import Any

from alpha_council.db.engine import Database
from alpha_council.models.evolution import ChallengerProposal
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.time import iso_utc, utc_now


class PromotionRefused(RuntimeError):
    """Raised when a promotion is attempted without its preconditions."""


class ChampionRegistry:
    def __init__(self, db: Database, evolution_cfg: dict[str, Any] | None = None):
        self.db = db
        self.cfg = evolution_cfg or {}

    # ---- champion ---------------------------------------------------

    async def ensure_champion(self, config_version: str,
                              config_snapshot: dict[str, Any],
                              strategy_id: str = "alpha_v2_5_c0") -> str:
        """Create the champion row if none exists. Returns its id."""
        current = await self.current_champion()
        if current:
            return current["strategy_id"]
        await self.db.execute(
            "INSERT INTO strategy_versions(strategy_id, parent_strategy_id, "
            "status, created_at, promoted_at, config_version, config_json, "
            "hypothesis, operator_approved) "
            "VALUES(?,NULL,'CHAMPION',?,?,?,?,?,1)",
            (strategy_id, iso_utc(), iso_utc(), config_version,
             json.dumps(config_snapshot, default=str),
             "live configuration at first activation"))
        await self.db.log_event("INFO", "champion", "CHAMPION_CREATED",
                                f"{strategy_id} from {config_version}")
        return strategy_id

    async def current_champion(self) -> dict[str, Any] | None:
        return await self.db.fetchone(
            "SELECT * FROM strategy_versions WHERE status='CHAMPION' "
            "ORDER BY created_at DESC LIMIT 1")

    async def champion_config(self) -> dict[str, Any]:
        row = await self.current_champion()
        if not row:
            return {}
        try:
            return json.loads(row["config_json"])
        except (ValueError, TypeError):
            return {}

    # ---- challenger -------------------------------------------------

    async def active_challenger(self) -> dict[str, Any] | None:
        return await self.db.fetchone(
            "SELECT * FROM strategy_versions WHERE status='CHALLENGER' "
            "ORDER BY created_at DESC LIMIT 1")

    async def active_challenger_proposal(self) -> dict[str, Any] | None:
        row = await self.active_challenger()
        if not row:
            return None
        return await self.db.fetchone(
            "SELECT * FROM challenger_proposals WHERE challenger_id=?",
            (row["strategy_id"],))

    async def store_challenger(self, proposal: ChallengerProposal,
                               challenger_config: dict[str, Any]) -> str:
        """Persist a VALIDATED proposal as the shadowing challenger.

        Refuses when a challenger is already active (max one, §11.3).
        Callers run change_validator first; this stores, it does not judge.
        """
        max_active = int(self.cfg.get("max_active_challengers", 1))
        existing = await self.active_challenger()
        if existing and max_active <= 1:
            raise PromotionRefused(
                f"challenger {existing['strategy_id']} is already shadowing; "
                "one hypothesis at a time")

        await self.db.execute(
            "INSERT INTO strategy_versions(strategy_id, parent_strategy_id, "
            "status, created_at, config_version, config_json, hypothesis, "
            "operator_approved) VALUES(?,?,'CHALLENGER',?,?,?,?,0)",
            (proposal.challenger_id, proposal.parent_champion_id,
             iso_utc(proposal.created_at),
             f"{proposal.parent_champion_id}+{proposal.challenger_id}",
             json.dumps(challenger_config, default=str),
             proposal.hypothesis))
        await self.db.execute(
            "INSERT OR REPLACE INTO challenger_proposals(challenger_id, "
            "parent_champion_id, created_at, hypothesis, "
            "evidence_summary_json, changes_json, expected_benefit, "
            "expected_failure_mode, minimum_shadow_observations, confidence, "
            "status) VALUES(?,?,?,?,?,?,?,?,?,?,'SHADOWING')",
            (proposal.challenger_id, proposal.parent_champion_id,
             iso_utc(proposal.created_at), proposal.hypothesis,
             json.dumps(proposal.evidence_summary),
             json.dumps([c.model_dump() for c in proposal.changes]),
             proposal.expected_benefit, proposal.expected_failure_mode,
             proposal.minimum_shadow_observations, proposal.confidence))
        await self.db.log_event(
            "INFO", "champion", "CHALLENGER_STORED",
            f"{proposal.challenger_id}: {proposal.hypothesis[:120]}",
            {"changes": len(proposal.changes)})
        return proposal.challenger_id

    async def record_rejected_proposal(self, proposal: ChallengerProposal,
                                       problems: list[str]) -> None:
        """A rejected proposal is still an audit record (§0.7)."""
        await self.db.execute(
            "INSERT OR REPLACE INTO challenger_proposals(challenger_id, "
            "parent_champion_id, created_at, hypothesis, "
            "evidence_summary_json, changes_json, expected_benefit, "
            "expected_failure_mode, minimum_shadow_observations, confidence, "
            "status) VALUES(?,?,?,?,?,?,?,?,?,?,'REJECTED')",
            (proposal.challenger_id, proposal.parent_champion_id,
             iso_utc(proposal.created_at), proposal.hypothesis,
             json.dumps({"evidence": proposal.evidence_summary,
                         "validator_problems": problems}),
             json.dumps([c.model_dump() for c in proposal.changes]),
             proposal.expected_benefit, proposal.expected_failure_mode,
             proposal.minimum_shadow_observations, proposal.confidence))

    async def next_challenger_id(self) -> str:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM strategy_versions")
        return f"alpha_v2_5_c{int((row or {}).get('n') or 0) + 1}"

    # ---- promotion (operator-gated; never scheduled) -----------------

    async def promote(self, challenger_id: str,
                      operator_approved: bool = False) -> None:
        """Make the challenger the champion. Operator approval mandatory.

        Competition mode never relaxes this: the scheduler has no code
        path here, and calling it without approval raises.
        """
        if not operator_approved:
            raise PromotionRefused(
                "promotion requires explicit operator approval "
                "(v2.5 §11.3); refuse to proceed without it")
        challenger = await self.db.fetchone(
            "SELECT * FROM strategy_versions WHERE strategy_id=? "
            "AND status='CHALLENGER'", (challenger_id,))
        if not challenger:
            raise PromotionRefused(f"{challenger_id} is not an active "
                                   "challenger")
        stamp = iso_utc()
        await self.db.execute(
            "UPDATE strategy_versions SET status='RETIRED', retired_at=? "
            "WHERE status='CHAMPION'", (stamp,))
        await self.db.execute(
            "UPDATE strategy_versions SET status='CHAMPION', promoted_at=?, "
            "operator_approved=1 WHERE strategy_id=?", (stamp, challenger_id))
        await self.db.execute(
            "UPDATE challenger_proposals SET status='PROMOTED' "
            "WHERE challenger_id=?", (challenger_id,))
        await self.db.log_event("WARN", "champion", "CHALLENGER_PROMOTED",
                                f"{challenger_id} is now the champion",
                                {"operator_approved": True})
