"""
Alpha Council v2.4 - configuration version store.

Every decision, candidate, and gate rejection references the config version
in force when it happened, so results stay attributable to the settings
that produced them. That reference is a foreign key, which means the parent
row has to exist before anything else is written.

The tier ladder also writes here: each escalation (Tier 1 -> 2 -> 3) creates
a new version row, so the audit trail shows exactly which thresholds were
active at each point in the session.

Place at: alpha_council/db/config_store.py
"""

from __future__ import annotations

import json
from typing import Any

from alpha_council.db.engine import Database, utc_now


async def ensure_config_version(db: Database, version: str,
                                scoring: dict[str, Any] | None = None,
                                risk: dict[str, Any] | None = None,
                                tier: int = 1,
                                note: str = "auto-created") -> str:
    """Create the config_versions row if absent. Returns the version.

    Idempotent: an existing version is left untouched, so re-running a scan
    never rewrites the configuration history.
    """
    row = await db.fetchone(
        "SELECT config_version FROM config_versions WHERE config_version=?",
        (version,),
    )
    if row:
        return version

    await db.execute(
        "INSERT INTO config_versions(config_version, activated_at, tier, "
        "scoring_json, risk_json, note) VALUES(?,?,?,?,?,?)",
        (version, utc_now(), tier,
         json.dumps(scoring or {}, default=str),
         json.dumps(risk or {}, default=str),
         note),
    )
    return version


async def active_config_version(db: Database) -> str | None:
    row = await db.fetchone(
        "SELECT config_version FROM config_versions "
        "WHERE deactivated_at IS NULL ORDER BY activated_at DESC LIMIT 1"
    )
    return row["config_version"] if row else None


async def record_tier_change(db: Database, base_version: str, new_tier: int,
                             scoring: dict[str, Any], risk: dict[str, Any],
                             reason: str) -> str:
    """Escalate the ladder and version the change.

    A tier change is a configuration change. Recording it as a new version
    is what makes 'this trade was taken under Tier 3 thresholds at 14:22'
    reconstructable after the fact.
    """
    stamp = utc_now()
    version = f"{base_version}-t{new_tier}-{stamp[11:19].replace(':', '')}"

    await db.execute(
        "UPDATE config_versions SET deactivated_at=? "
        "WHERE deactivated_at IS NULL", (stamp,),
    )
    await db.execute(
        "INSERT INTO config_versions(config_version, activated_at, tier, "
        "scoring_json, risk_json, note) VALUES(?,?,?,?,?,?)",
        (version, stamp, new_tier,
         json.dumps(scoring, default=str), json.dumps(risk, default=str),
         reason),
    )
    await db.log_event("INFO", "config_store", "TIER_CHANGE",
                       f"tier -> {new_tier}: {reason}",
                       {"config_version": version, "tier": new_tier})
    return version


async def config_history(db: Database) -> list[dict[str, Any]]:
    return await db.fetchall(
        "SELECT config_version, activated_at, deactivated_at, tier, note "
        "FROM config_versions ORDER BY activated_at"
    )
